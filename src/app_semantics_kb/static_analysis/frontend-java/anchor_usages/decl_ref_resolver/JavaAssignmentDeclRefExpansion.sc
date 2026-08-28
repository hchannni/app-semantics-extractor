import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

object JavaAssignmentDeclRefExpansion {
  import JavaAssignmentDeclRefCore.typeDeclOf

  private def sameMethodIdentifiers(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declName = declaration.name
    Option(declaration).collect { case node: AstNode => node }
      .flatMap(_.start.repeat(_.astParent)(_.emit).collectAll[Method].headOption)
      .map { method =>
        method.ast.collectAll[Identifier]
          .nameExact(declName)
          .filterNot(_.id == declaration.id)
          .collect { case node: AstNode => node }
          .l
      }
      .getOrElse(Nil)
  }

  private def nestedMethodIdentifiers(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declName = declaration.name
    Option(declaration).collect { case node: AstNode => node }
      .flatMap(_.start.repeat(_.astParent)(_.emit).collectAll[Method].headOption)
      .map { method =>
        method.ast.collectAll[Method]
          .flatMap(_.ast.collectAll[Identifier].nameExact(declName).l)
          .filterNot(_.id == declaration.id)
          .collect { case node: AstNode => node }
          .l
      }
      .getOrElse(Nil)
  }

  private def lambdaMethodIdentifiers(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declName = declaration.name
    Option(declaration).collect { case node: AstNode => node }
      .flatMap(_.start.repeat(_.astParent)(_.emit).collectAll[Method].headOption)
      .map { method =>
        val lambdaFullNames =
          method.ast.collectAll[MethodRef].map(_.methodFullName).distinct

        lambdaFullNames
          .flatMap(fullName => cpg.method.fullNameExact(fullName).ast.collectAll[Identifier].nameExact(declName).l)
          .filterNot(_.id == declaration.id)
          .collect { case node: AstNode => node }
          .distinctBy(_.id)
          .l
      }
      .getOrElse(Nil)
  }

  private def closureBindingIdentifiers(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val declId = declaration.id
    cpg.identifier
      .nameExact(declaration.name)
      .filterNot(_.id == declId)
      .filter { id =>
        id._capturedByIn
          .collectAll[ClosureBinding]
          .flatMap(_._refOut.collectAll[Declaration])
          .id
          .contains(declId)
      }
      .collect { case node: AstNode => node }
      .l
  }

  private def receiverRootIdentifier(node: AstNode): Option[Identifier] =
    node.start
      .repeat(_.astParent)(_.emit)
      .collectAll[Call]
      .find(call => Option(call.name).contains("<operator>.fieldAccess"))
      .flatMap(_.receiver.collectFirst { case id: Identifier => id })

  private def receiverContainsMember(node: AstNode, memberName: String): Boolean = {
    def walk(expr: Expression): Boolean = expr match {
      case id: Identifier =>
        Option(id.name).contains(memberName)
      case field: FieldIdentifier =>
        Option(field.canonicalName).contains(memberName) || Option(field.code).contains(memberName)
      case call: Call =>
        call.receiver.exists(walk) ||
          call.astChildren.collect { case child: Expression => child }.exists(walk)
      case _ =>
        expr.astChildren.collect { case child: Expression => child }.exists(walk)
    }

    node match {
      case call: Call => call.receiver.exists(walk)
      case expr: Expression => walk(expr)
      case _ => false
    }
  }

  private def receiverTypeMatches(call: Call, ownerType: String): Boolean = {
    val receiverTypes = call.receiver.typ.fullName.map(Option(_).getOrElse("")).l
    receiverTypes.contains(ownerType) ||
      receiverRootIdentifier(call).exists(id => Option(id.typ.fullName).contains(ownerType))
  }

  private def memberReferences(member: Member)(implicit cpg: Cpg): List[AstNode] = {
    val declName = member.name
    typeDeclOf(member) match {
      case Some(typeDecl) =>
        val ownerType = Option(typeDecl.fullName).getOrElse("")
        val ownerId = typeDecl.id

        val internalIdentifiers = typeDecl.ast.collectAll[Identifier]
          .nameExact(declName)
          .filterNot(_.id == member.id)
          .collect { case node: AstNode => node }
          .l

        val internalFields = typeDecl.ast.collectAll[FieldIdentifier]
          .filter(field => Option(field.canonicalName).contains(declName) || Option(field.code).contains(declName))
          .filterNot(_.id == member.id)
          .collect { case node: AstNode => node }
          .l

        val externalIdentifiersByName =
          cpg.identifier
            .nameExact(declName)
            .filterNot(_.id == member.id)
            .filter(node => typeDeclOf(node).forall(_.id != ownerId))
            .filter(id => ownerType.nonEmpty && Option(id.typ.fullName).contains(ownerType))
            .collect { case node: AstNode => node }
            .l

        val externalFieldsByReceiverType =
          cpg.fieldIdentifier
            .filter(field => Option(field.canonicalName).contains(declName) || Option(field.code).contains(declName))
            .filterNot(_.id == member.id)
            .filter(node => typeDeclOf(node).forall(_.id != ownerId))
            .flatMap { field =>
              field.start
                .repeat(_.astParent)(_.emit)
                .collectAll[Call]
                .find(call => Option(call.name).contains("<operator>.fieldAccess"))
                .flatMap { call =>
                  val receiverType = call.argument(1)
                    .collectFirst { case expr: Expression => expr }
                    .flatMap(_.typ.fullName.headOption)
                    .getOrElse("ANY")
                  val exactMatch = ownerType.nonEmpty && receiverType == ownerType
                  val looseMatch = receiverType == "ANY" || receiverType == "UNKNOWN" || receiverType.isEmpty
                  if (exactMatch || looseMatch) Some(field: AstNode) else None
                }
            }
            .l

        val callReceiverRefs =
          cpg.call
            .filterNot(_.id == member.id)
            .filter(node => typeDeclOf(node).forall(_.id != ownerId))
            .filter(call => receiverContainsMember(call, declName))
            .filter(call => ownerType.nonEmpty && receiverTypeMatches(call, ownerType))
            .collect { case node: AstNode => node }
            .l

        (
          internalIdentifiers ++
            internalFields ++
            externalIdentifiersByName ++
            externalFieldsByReceiverType ++
            callReceiverRefs
        ).distinctBy(_.id)
      case None => Nil
    }
  }

  def referencesForDeclaration(declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val directRefs = declaration.start
      ._refIn
      .collect { case node: AstNode if node.isInstanceOf[Identifier] || node.isInstanceOf[FieldIdentifier] => node }
      .l

    val fallbackRefs = declaration match {
      case _: Local | _: MethodParameterIn =>
        sameMethodIdentifiers(declaration) ++
          nestedMethodIdentifiers(declaration) ++
          lambdaMethodIdentifiers(declaration) ++
          closureBindingIdentifiers(declaration)
      case member: Member =>
        memberReferences(member)
      case _ => Nil
    }

    (directRefs ++ fallbackRefs).distinctBy(_.id).sortBy(_.id)
  }
}
