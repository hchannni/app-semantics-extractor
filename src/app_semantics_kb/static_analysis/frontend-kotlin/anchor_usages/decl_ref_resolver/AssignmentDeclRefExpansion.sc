import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.codepropertygraph.generated.Operators
import io.shiftleft.semanticcpg.language.*
import io.joern.dataflowengineoss.language.*
import io.joern.joerncli.console.Joern.context

object AssignmentDeclRefExpansion {
  import AssignmentDeclRefCore.typeDeclOf

  // REF 에지가 누락된 로컬/파라미터를 보완하기 위한 이름 기반 탐색
  def findRefsByScopeName(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declName = declaration.name

    val methodOpt =
      Option(declaration).collect { case n: AstNode => n }.flatMap { n =>
        n.start
          .repeat(_.astParent)(_.emit)
          .collectAll[Method]
          .headOption
      }

    methodOpt match {
      case Some(method) =>
        method.ast
          .collectAll[Identifier]
          .nameExact(declName)
          .filterNot(_.id == declaration.id)
          .distinctBy(_.id)
          .l
      case None => Nil
    }
  }

  // 람다/중첩 메서드 내부에서 같은 이름으로 캡처되어 사용되는 식별자를 찾기 위한 보조 로직
  def findRefsInNestedMethods(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declName = declaration.name
    val enclosingMethodOpt =
      Option(declaration).collect { case n: AstNode => n }.flatMap { n =>
        n.start
          .repeat(_.astParent)(_.emit)
          .collectAll[Method]
          .headOption
      }

    enclosingMethodOpt
      .map { method =>
        method.ast
          .collectAll[Method]
          .flatMap(_.ast.collectAll[Identifier].nameExact(declName).l)
          .filterNot(_.id == declaration.id)
          .distinctBy(_.id)
          .l
      }
      .getOrElse(Nil)
  }

  // 람다가 별도 Method(MethodRef/MethodInst)로 분리된 경우까지 내려가 동일 이름 식별자를 수집
  def findRefsInLambdaMethods(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declName = declaration.name
    val enclosingMethodOpt =
      Option(declaration).collect { case n: AstNode => n }.flatMap { n =>
        n.start
          .repeat(_.astParent)(_.emit)
          .collectAll[Method]
          .headOption
      }

    enclosingMethodOpt
      .map { method =>
        val lambdaFullNames =
          method.ast.collectAll[MethodRef].map(_.methodFullName).distinct

        lambdaFullNames
          .flatMap(fn => cpg.method.fullNameExact(fn).ast.collectAll[Identifier].nameExact(declName).l)
          .filterNot(_.id == declaration.id)
          .distinctBy(_.id)
          .l
      }
      .getOrElse(Nil)
  }

  // ClosureBinding 기반으로 외부 선언을 캡처한 내부 식별자를 수집
  def findRefsByClosureBinding(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declId = declaration.id
    cpg.identifier
      .nameExact(declaration.name)
      .filterNot(_.id == declId)
      .filter { id =>
        id._capturedByIn
          .collectAll[ClosureBinding]
          .flatMap(_._refOut.collectAll[Declaration])
          .id.contains(declId)
      }
      .collect { case id: AstNode => id }
      .l
  }

  // 데이터플로우 기반으로 선언 -> 사용 도달 여부를 통해 참조를 보완
  def findRefsByDataflow(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declId = declaration.id
    val sources: List[StoredNode] = declaration match {
      case d: Declaration => List(d)
      case _ => Nil
    }
    val sinks =
      cpg.identifier
        .nameExact(declaration.name)
        .filterNot(_.id == declId)

    sinks
      .reachableBy(sources)
      .collect { case n: AstNode => n }
      .distinctBy(_.id)
      .l
  }

  // 주어진 AST 노드에서 부모 방향으로 올라가며, 경로상에서 만나는 fieldAccess 호출 하나를 골라, 그 호출의 receiver 쪽에 바로 나오는 Identifier 하나를 꺼냄
  // 여기서 receiver는 Joern이 Call 노드에 붙이는 개념으로, “그 호출(연산)이 붙어 있는 쪽 표현식” 즉 점(.) 왼쪽·괄호 앞에 해당하는 AST 조각을 가리킵니다.
  // E.g. a.doSomething() -> a가 receiver (메서드 호출의 케이스)
  // E.g.2 binding.foo -> binding이 receiver (<operator>.fieldAccess의 케이스)
  // E.g.3 a.b.c -> 안쪽 fieldAccess의 receiver는 a, 바깥쪽 fieldAccess의 receiver는 안쪽 fieldAccess Call (중첩 케이스)
  private def receiverRootIdentifier(node: AstNode): Option[Identifier] =
    node.start
      .repeat(_.astParent)(_.emit)
      .collectAll[Call]
      .find(c => Option(c.name).contains(Operators.fieldAccess) || Option(c.name).contains("<operator>.fieldAccess"))
      .flatMap(_.receiver.collectFirst { case id: Identifier => id })

  private def receiverContainsMember(node: AstNode, memberName: String): Boolean = {
    def walk(expr: Expression): Boolean = expr match {
      case id: Identifier =>
        Option(id.name).contains(memberName)
      case fi: FieldIdentifier =>
        Option(fi.canonicalName).contains(memberName) || Option(fi.code).contains(memberName)
      case call: Call =>
        call.receiver.exists(walk) ||
        call.astChildren.collect { case e: Expression => e }.exists(walk)
      case _ =>
        expr.astChildren.collect { case e: Expression => e }.exists(walk)
    }

    node match {
      case call: Call => call.receiver.exists(walk)
      case expr: Expression => walk(expr)
      case _ => false
    }
  }

  private def receiverTypeMatches(call: Call, ownerType: String): Boolean = {
    val recvTypes = call.receiver.typ.fullName.map(Option(_).getOrElse("")).l
    recvTypes.contains(ownerType) ||
    receiverRootIdentifier(call).exists(id => Option(id.typ.fullName).contains(ownerType))
  }

  def findRefsForMember(member: Member)(implicit cpg: Cpg): List[AstNode] = {
    val declName = member.name
    val typeDeclOpt = typeDeclOf(member)

    typeDeclOpt match {
      case Some(typeDecl) =>
        val ownerType = Option(typeDecl.fullName).getOrElse("")
        val ownerId = typeDecl.id

        val internalIds = typeDecl.ast
          .collectAll[Identifier]
          .nameExact(declName)
          .filterNot(_.id == member.id)
          .filterNot { id =>
            id.refsTo.collectFirst { case _: Local | _: MethodParameterIn => true }.getOrElse(false)
          }
          .l

        val internalFields = typeDecl.ast
          .collectAll[FieldIdentifier]
          .filter(fi => Option(fi.canonicalName).contains(declName) || Option(fi.code).contains(declName))
          .filterNot(_.id == member.id)
          .l

        val externalIdsByName =
          cpg.identifier
            .nameExact(declName)
            .filterNot(_.id == member.id)
            .filter(n => typeDeclOf(n).forall(_.id != ownerId))
            .filter(id => Option(id.typ.fullName).contains(ownerType))
            .l

        val externalFieldsByReceiverType =
          cpg.fieldIdentifier
            .filter(fi => Option(fi.canonicalName).contains(declName) || Option(fi.code).contains(declName))
            .filterNot(_.id == member.id)
            .filter(n => typeDeclOf(n).forall(_.id != ownerId))
            .flatMap { fi =>
              fi.start
                .repeat(_.astParent)(_.emit)
                .collectAll[Call]
                .find(c => Option(c.name).contains(Operators.fieldAccess) || Option(c.name).contains("<operator>.fieldAccess"))
                .flatMap { call =>
                  val recvOpt = call.argument(1).collectFirst { case e: Expression => e }
                  val recvType = recvOpt.flatMap(_.typ.fullName.headOption).getOrElse("ANY")
                  val typeMatch = ownerType.nonEmpty && recvType == ownerType
                  val looseMatch = recvType == "ANY" || recvType == "UNKNOWN" || recvType.isEmpty
                  if (typeMatch || looseMatch) Some(fi) else None
                }
            }
            .l

        val callReceiverRefs =
          cpg.call
            .filterNot(_.id == member.id)
            .filter(n => typeDeclOf(n).forall(_.id != ownerId))
            .filter(c => receiverContainsMember(c, declName))
            .filter(c => ownerType.nonEmpty && receiverTypeMatches(c, ownerType))
            .l

        (internalIds ++ internalFields ++ externalIdsByName ++ externalFieldsByReceiverType ++ callReceiverRefs)
          .distinctBy(_.id)
      case None => Nil
    }
  }

  /**
   * 특정 선언 노드들에 대한 모든 참조 노드들을 찾는다.
   */
  def referencesForDeclaration(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val directRefs = declaration.start
      ._refIn
      .collect { case node: AstNode if node.isInstanceOf[Identifier] || node.isInstanceOf[FieldIdentifier] => node }
      .l

    val fallbackRefs = declaration match {
      case _: Local | _: MethodParameterIn =>
        (
          findRefsByScopeName(declaration) ++
          findRefsInNestedMethods(declaration) ++
          findRefsInLambdaMethods(declaration) ++
          findRefsByClosureBinding(declaration) ++
          findRefsByDataflow(declaration)
        ).distinctBy(_.id)
      case m: Member => findRefsForMember(m)
      case _ => Nil
    }

    (directRefs ++ fallbackRefs).distinctBy(_.id)
  }
}
