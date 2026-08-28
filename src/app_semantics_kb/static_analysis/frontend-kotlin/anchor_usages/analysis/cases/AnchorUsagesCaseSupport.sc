import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

import scala.annotation.tailrec
import scala.util.Try

object AnchorUsagesCaseSupport {
  import AnchorUsagesModel.*
  import AnchorUsagesUiSignals.*

  private val wrapperOperators = Set(
    "<operator>.cast",
    "<operator>.not",
    "<operator>.logicalNot",
    "<operator>.expressionList",
    "<operator>.ref"
  )

  @tailrec
  def effectiveParent(node: AstNode): Option[AstNode] = {
    val parent = node.astParent.collect { case astNode: AstNode => astNode }.headOption
    parent match {
      case Some(call: Call) if wrapperOperators.contains(call.name) => effectiveParent(call)
      case other => other
    }
  }

  def loadNode(nodeId: Long)(implicit cpg: Cpg): Option[AstNode] =
    Try(cpg.graph.node(nodeId)).toOption.collect { case node: AstNode => node }

  def ancestorsOfType[T](node: AstNode)(implicit tag: scala.reflect.ClassTag[T]): Iterator[T] =
    node.start.repeat(_.astParent)(_.emit).collectAll[T]

  def firstAncestorOfType[T](node: AstNode)(implicit tag: scala.reflect.ClassTag[T]): Option[T] =
    ancestorsOfType[T](node).headOption

  def isOperatorCall(call: Call): Boolean =
    Option(call.name).exists(_.startsWith("<operator>."))

  private def enclosingNonOperatorCall(node: AstNode): Option[Call] =
    ancestorsOfType[Call](node)
      .filterNot(isOperatorCall)
      .headOption

  def contextCallOf(node: AstNode): Option[Call] = node match {
    case c: Call if !isOperatorCall(c) => Some(c)
    case _ => enclosingNonOperatorCall(node)
  }

  def subtreeContains(root: AstNode, targetId: Long): Boolean =
    root.id == targetId || root.ast.collect { case n: AstNode => n }.id.l.contains(targetId)

  private def enclosingMethodFullNameOf(node: AstNode): Option[String] =
    firstAncestorOfType[Method](node)
      .flatMap(m => Option(m.methodFullName))

  def callerCallSitesOf(node: AstNode)(implicit cpg: Cpg): List[AstNode] =
    firstAncestorOfType[Method](node)
      .toList
      .flatMap { method =>
        Option(method.fullName).toList.flatMap { methodFullName =>
          cpg.call
            .methodFullNameExact(methodFullName)
            .l
            .collect { case call: Call => call: AstNode }
        }
      }

  def parentMethodOf(methodFullName: String)(implicit cpg: Cpg): Option[String] = {
    if (methodFullName.contains("<lambda>") || methodFullName.contains("<anonymous>")) {
      val lambdaIndex = methodFullName.indexOf(".<lambda>")
      val anonymousIndex = methodFullName.indexOf(".<anonymous>")
      val splitIndex = if (lambdaIndex >= 0) lambdaIndex else anonymousIndex

      if (splitIndex >= 0) {
        val beforeLambda = methodFullName.substring(0, splitIndex)
        cpg.method.fullName.filter(_.startsWith(beforeLambda + ":")).headOption
          .orElse(Some(beforeLambda))
      } else None
    } else None
  }

  def sameEnclosingMethod(call: Call, node: AstNode)(implicit cpg: Cpg): Boolean = {
    val callMethodOpt = enclosingMethodFullNameOf(call)
    val nodeMethodOpt = enclosingMethodFullNameOf(node)

    (callMethodOpt, nodeMethodOpt) match {
      case (Some(callMethod), Some(nodeMethod)) =>
        if (callMethod == nodeMethod) {
          true
        } else {
          val callParent = parentMethodOf(callMethod)
          val nodeParent = parentMethodOf(nodeMethod)

          callParent.contains(nodeMethod) || nodeParent.contains(callMethod) ||
            callParent == nodeParent && callParent.isDefined
        }
      case _ => false
    }
  }

  private def receiverRoots(call: Call): List[AstNode] = {
    val arg0 = call.argumentOption(0).collect { case a: AstNode => a }.toList
    if (arg0.nonEmpty) arg0
    else {
      call.astChildren
        .collect { case a: AstNode => a }
        .filterNot(_.isInstanceOf[MethodRef])
        .filterNot(_.isInstanceOf[Block])
        .l
    }
  }

  def isWithinReceiver(call: Call, node: AstNode)(implicit cpg: Cpg): Boolean =
    sameEnclosingMethod(call, node) &&
      receiverRoots(call).exists(recv => subtreeContains(recv, node.id))

  private def argumentOperands(call: Call): List[AstNode] =
    call.argument
      .collect { case arg: Expression if arg.argumentIndex > 0 => arg: AstNode }
      .l

  def isWithinArgument(call: Call, node: AstNode)(implicit cpg: Cpg): Boolean =
    sameEnclosingMethod(call, node) &&
      argumentOperands(call).exists(arg => subtreeContains(arg, node.id))

  private def nearestStatementNode(node: AstNode): Option[AstNode] = {
    @tailrec
    def loop(cur: Option[AstNode]): Option[AstNode] = cur match {
      case Some(cfg: CfgNode) => Some(cfg)
      case Some(other) => loop(other.astParent.collect { case a: AstNode => a }.headOption)
      case None => None
    }
    loop(Some(node))
  }

  def statementCallOf(node: AstNode): Option[Call] =
    nearestStatementNode(node).collect { case c: Call => c }

  private def captureStatementCode(node: AstNode): String =
    nearestStatementNode(node)
      .flatMap(n => Option(n.code))
      .orElse(Option(node.code))
      .getOrElse("")

  def preferredCarrierCall(node: AstNode): Option[Call] =
    contextCallOf(node)
      .orElse(statementCallOf(node))
      .orElse(node match {
        case c: Call => Some(c)
        case _ => None
      })

  private val falsePositivePatterns = Set(
    "<operator>",
    "tmp_",
    "<alloc>",
    "val ",
    "var "
  )

  def isFalsePositive(code: String): Boolean = {
    val trimmed = code.trim
    falsePositivePatterns.exists(trimmed.startsWith) ||
      trimmed.contains("<alloc>") ||
      (trimmed.startsWith("this.") && !trimmed.contains("("))
  }

  private def cleanUsageCode(code: String): String = {
    val trimmed = code.trim
    if (isFalsePositive(trimmed)) "" else trimmed
  }

  def buildScopeUsage(scopeCall: Call): SemanticUsage =
    SemanticUsage(
      usageKind = UsageKind.Other,
      nodeLabel = scopeCall.label,
      code = Option(scopeCall.code).getOrElse(""),
      sourceLocation = SourceLocation.fromFileLine(scopeCall.filename, scopeCall.lineNumber.getOrElse(-1)),
      methodFullName = scopeCall.method.methodFullName,
      nodeId = scopeCall.id,
      usageMethodFullName = scopeCall.method.methodFullName
    )

  def buildUsage(anchor: ViewAnchor, kind: UsageKind, call: Call): SemanticUsage =
    val usageMethodFullName = call.method.methodFullName
    SemanticUsage(
      usageKind = kind,
      nodeLabel = call.label,
      code = cleanUsageCode(captureStatementCode(call)),
      sourceLocation = SourceLocation.fromFileLine(call.filename, call.lineNumber.getOrElse(-1)),
      methodFullName = call.method.methodFullName,
      nodeId = call.id,
      usageMethodFullName = usageMethodFullName
    )

  def classifyCallAsUsages(anchor: ViewAnchor, call: Call): List[SemanticUsage] =
    classifyCallName(call.name)
      .map(sig => buildUsage(anchor, usageKindOf(sig), call))
      .toList

  private def directCalleeMethods(call: Call)(implicit cpg: Cpg): List[Method] = {
    val byFullName = Option(call.methodFullName)
      .toList
      .flatMap(fn => cpg.method.fullNameExact(fn).l)

    val bySameTypeName = Option(call.method).toList.flatMap { callerMethod =>
      callerMethod.typeDecl.headOption.toList.flatMap { typeDecl =>
        typeDecl.method.nameExact(call.name).l
      }
    }

    val bySameFileName = Option(call.file.name.headOption.getOrElse("")).filter(_.nonEmpty).toList.flatMap { file =>
      cpg.method
        .nameExact(call.name)
        .filter(method => method.file.name.headOption.contains(file))
        .l
    }

    (byFullName ++ bySameTypeName ++ bySameFileName)
      .filterNot(_.id == call.method.id)
      .distinctBy(_.id)
  }

  private def parameterReferenceNodes(
    method: Method,
    parameter: MethodParameterIn
  )(implicit cpg: Cpg): List[AstNode] = {
    val paramName = Option(parameter.name).getOrElse("")
    if (paramName.isEmpty) return Nil

    val directRefs =
      parameter.start._refIn.collect { case node: AstNode => node }.l

    val localRefs =
      method.ast
        .collectAll[Identifier]
        .nameExact(paramName)
        .filterNot(_.id == parameter.id)
        .l

    val lambdaRefs = {
      val lambdaFullNames = method.ast.collectAll[MethodRef].map(_.methodFullName).l.distinct
      lambdaFullNames.flatMap { fullName =>
        cpg.method
          .fullNameExact(fullName)
          .ast
          .collectAll[Identifier]
          .nameExact(paramName)
          .filterNot(_.id == parameter.id)
          .l
      }
    }

    val closureRefs =
      cpg.identifier
        .nameExact(paramName)
        .filterNot(_.id == parameter.id)
        .filter { id =>
          id._capturedByIn
            .collectAll[ClosureBinding]
            .flatMap(_._refOut.collectAll[Declaration])
            .id
            .contains(parameter.id)
        }
        .collect { case node: AstNode => node }
        .l

    (directRefs ++ localRefs ++ lambdaRefs ++ closureRefs).distinctBy(_.id)
  }

  def interProceduralArgumentTargets(
    anchorRef: AstNode,
    call: Call
  )(implicit cpg: Cpg): List[AstNode] = {
    val argumentIndexes =
      call.argument
        .collect {
          case arg: Expression
              if arg.argumentIndex > 0 && subtreeContains(arg, anchorRef.id) =>
            arg.argumentIndex
        }
        .l
        .distinct

    if (argumentIndexes.isEmpty) return Nil

    directCalleeMethods(call)
      .flatMap { callee =>
        argumentIndexes.flatMap { argumentIndex =>
          callee.parameter
            .filter(_.index == argumentIndex)
            .l
            .flatMap(param => parameterReferenceNodes(callee, param))
        }
      }
      .distinctBy(_.id)
  }

  private def isViewType(typeFullName: String): Boolean = {
    val lower = typeFullName.toLowerCase
    lower.contains("android.view.view") ||
    lower.contains("android.view.viewgroup") ||
    lower.contains("android.widget.") ||
    lower.contains("androidx.") ||
    lower.contains("com.google.android.material.")
  }

  def isViewFieldAccess(fieldAccess: Call, anchor: ViewAnchor): Boolean = {
    fieldAccess.argumentOption(1) match {
      case Some(receiver) =>
        val receiverType = receiver match {
          case id: Identifier =>
            Try(id.typeFullName).toOption.getOrElse("")
          case call: Call =>
            Try(call.typeFullName).toOption.getOrElse("")
          case _ =>
            ""
        }

        if (receiverType.nonEmpty && receiverType != "ANY") {
          if (anchor.declarationScope.contains("MEMBER")) true
          else isViewType(receiverType)
        } else {
          true
        }
      case None => false
    }
  }

  def propertyAccessOf(node: AstNode): Option[(Call, Option[String])] = {
    val faOpt: Option[Call] = node match {
      case fi: FieldIdentifier =>
        fi.astParent.collect { case c: Call if c.name == "<operator>.fieldAccess" => c }.headOption
      case c: Call if c.name == "<operator>.fieldAccess" =>
        Some(c)
      case _ =>
        ancestorsOfType[Call](node)
          .filter(_.name == "<operator>.fieldAccess")
          .filter { fa =>
            fa.argumentOption(1).collect { case a: AstNode => a }.exists(recv => subtreeContains(recv, node.id))
          }
          .headOption
    }

    faOpt.map { fa =>
      val prop = fa.argument(2).collect { case fi: FieldIdentifier => fi.canonicalName }.headOption
      fa -> prop
    }
  }

  def outerFieldAccessOf(innerFieldAccess: Call): Option[Call] =
    innerFieldAccess.astParent.collect { case c: Call => c }.headOption
      .filter { parent =>
        parent.name == "<operator>.fieldAccess" &&
          parent.argumentOption(1).exists(arg => arg.id == innerFieldAccess.id)
      }

  def assignmentForLhs(lhs: Call): Option[Call] =
    lhs.astParent.collect { case c: Call => c }.headOption
      .filter(p => p.name == "<operator>.assignment" && p.argumentOption(1).exists(arg => arg.id == lhs.id))

  /**
   * RETURN 앵커 중 by-lazy 패턴(`val foo by lazy { findByViewId(...) }`)에 대한 fallback 타겟 탐색.
   *
   * 기존 구현(AST Identifier 스캔)은 다음 두 이유로 실패 가능:
   *   1. Joern의 lambda 메서드 이름이 `<lambda>`를 포함하지 않을 수 있음
   *   2. outer method의 `.ast`가 nested lambda 내부 Identifier를 포함하지 않을 수 있음
   *
   * 개선된 전략: `cpg.local` 기반으로 Local 선언 노드에서 직접 참조 Identifier를 추적.
   *   1. node의 enclosing method (lambda 또는 일반 메서드) 식별
   *   2. parentMethodOf로 outer method 이름 결정 (lambda → parent, 일반 → self)
   *   3. outer method 범위의 `cpg.local` 중 viewType 매칭 Local 탐색
   *   4. `local.referencingIdentifiers`로 모든 참조 Identifier 수집
   *      (nested lambda 내부 참조도 포함)
   *
   * 타입 기반 휴리스틱이므로 같은 outer method에 동일 타입 local이 여럿 있으면
   * over-match 가능하나, 단일 lazy property 패턴에서는 충분히 정확하다.
   */
  def lazyPropertyFallbackTargets(node: AstNode, viewType: String)(implicit cpg: Cpg): List[AstNode] = {
    val enclosingMethodOpt = firstAncestorOfType[Method](node)

    enclosingMethodOpt.toList.flatMap { enclosingMethod =>
      val enclosingFN = Option(enclosingMethod.fullName).getOrElse("")
      // lambda이면 outer method, 아니면 self (by lazy가 outer method에 직접 있는 경우)
      val targetFN = parentMethodOf(enclosingFN).getOrElse(enclosingFN)
      val simpleType = viewType.split("\\.").lastOption.getOrElse(viewType)

      if (simpleType.isEmpty) Nil
      else
        cpg.local
          .filter { local =>
            // 타입 매칭: contains(simpleType)으로 `Lazy<LinearLayout>` 형태도 허용
            val localType = Try(local.typeFullName).toOption.getOrElse("")
            localType.nonEmpty && localType != "ANY" && localType.contains(simpleType)
          }
          .filter { local =>
            // Local이 target method 범위에 속하는지 확인
            firstAncestorOfType[Method](local)
              .flatMap(m => Option(m.fullName))
              .contains(targetFN)
          }
          .flatMap { local =>
            // 이 Local을 참조하는 모든 Identifier 노드 수집 (nested lambda 포함)
            Try(local.referencingIdentifiers.l).getOrElse(
              Try(local._refIn.collect { case id: Identifier => id }.toList).getOrElse(Nil)
            )
          }
          .map(id => id: AstNode)
          .l
          .distinctBy(_.id)
    }
  }

  private val scopeFunctionNames = Set("apply", "run", "also", "let", "with")

  def isScopeFunction(call: Call): Boolean =
    scopeFunctionNames.exists { name =>
      Option(call.name).exists(_.contains(name)) ||
        Option(call.methodFullName).exists(_.contains(name))
    }
}
