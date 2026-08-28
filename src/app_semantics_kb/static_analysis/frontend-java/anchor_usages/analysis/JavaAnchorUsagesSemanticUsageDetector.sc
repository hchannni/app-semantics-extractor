import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

object JavaAnchorUsagesSemanticUsageDetector {
  import JavaAnchorUsagesModel.*

  private val ignoredCallNames = Set(
    "<operator>.assignment",
    "<operator>.fieldaccess",
    "<operator>.indirectindexaccess",
    "<operator>.indexaccess",
    "<operator>.cast"
  )
  private val readOperatorNames = Set(
    "<operator>.equals",
    "<operator>.notequals",
    "<operator>.logicaland",
    "<operator>.logicalor",
    "<operator>.lessthan",
    "<operator>.lessequalsthan",
    "<operator>.greaterthan",
    "<operator>.greaterequalsthan"
  )

  private def isIgnored(call: Call): Boolean =
    ignoredCallNames.contains(Option(call.name).getOrElse("").toLowerCase)

  private def usageFromCall(
    call: Call,
    forcedKind: Option[UsageKind] = None,
    anchor: Option[ViewAnchor] = None,
    target: Option[AstNode] = None
  )(implicit cpg: Cpg): SemanticUsage = {
    val structuralKind =
      for {
        viewAnchor <- anchor
        targetNode <- target
        kind <- structuralUsageKind(viewAnchor, targetNode, call)
      } yield kind
    val kind = forcedKind.orElse(structuralKind).getOrElse(JavaAnchorUsagesUiSignals.classify(call))
    SemanticUsage(
      usageKind = kind,
      nodeLabel = call.label,
      code = Option(call.code).getOrElse(""),
      sourceLocation = JavaAnchorUsagesScope.locationOf(call),
      methodFullName = JavaAnchorUsagesScope.enclosingMethodFullName(call),
      nodeId = call.id,
      usageMethodFullName = JavaAnchorUsagesScope.enclosingMethodFullName(call)
    )
  }

  private def expressionArguments(call: Call): List[Expression] =
    call.argument.collect { case expression: Expression => expression }.l

  private def targetIsReceiverOf(call: Call, node: AstNode): Boolean = {
    val receiverContains =
      call.receiver.collectAll[Expression].l.exists(expr => containsTargetNode(expr, node))
    val argumentZeroContains =
      expressionArguments(call).exists(expr => expr.argumentIndex == 0 && containsTargetNode(expr, node))
    receiverContains || argumentZeroContains
  }

  private def isAssignmentRhs(call: Call, node: AstNode): Boolean =
    Option(call.name).contains("<operator>.assignment") &&
      expressionArguments(call).exists(expr => expr.argumentIndex > 1 && containsTargetNode(expr, node))

  private def isNonReceiverArgumentRead(call: Call, node: AstNode): Boolean =
    !isStructuralOperator(call) &&
      !targetIsReceiverOf(call, node) &&
      expressionArguments(call).exists(expr => expr.argumentIndex > 0 && containsTargetNode(expr, node))

  private def isReturnContext(node: AstNode): Boolean =
    JavaAnchorUsagesScope.firstAncestorOfType[Return](node).nonEmpty

  private def isReadOperator(call: Call): Boolean =
    readOperatorNames.contains(Option(call.name).getOrElse("").toLowerCase)

  private def isReadContext(call: Call, node: AstNode): Boolean =
    isAssignmentRhs(call, node) ||
      isNonReceiverArgumentRead(call, node) ||
      isReturnContext(node) ||
      isReadOperator(call)

  private def structuralUsageKind(anchor: ViewAnchor, node: AstNode, call: Call): Option[UsageKind] =
    if (JavaAnchorUsagesUiSignals.isListenerRegistration(call)) Some(UsageKind.Listener)
    else if (targetIsReceiverOf(call, node) && JavaAnchorUsagesUiSignals.isReceiverMutator(call, anchor.viewType)) {
      Some(UsageKind.Setter)
    } else if (targetIsReceiverOf(call, node) && JavaAnchorUsagesUiSignals.isGetterLike(call)) {
      Some(UsageKind.Getter)
    } else if (isReadContext(call, node)) {
      Some(UsageKind.Getter)
    } else None

  private def isCallbackCarrierCall(call: Call): Boolean = {
    val code = Option(call.code).getOrElse("")
    val lowerCode = code.toLowerCase
    JavaAnchorUsagesUiSignals.classify(call) == UsageKind.Listener &&
      (
        call.argument.collectAll[MethodRef].nonEmpty ||
          call.astChildren.collectAll[MethodRef].nonEmpty ||
          call.argument.collectAll[Block].nonEmpty ||
          code.contains("->") ||
          (lowerCode.contains("new ") && lowerCode.contains("listener"))
      )
  }

  private def callbackCarrierCalls(node: AstNode)(implicit cpg: Cpg): List[Call] = {
    val direct = JavaAnchorUsagesScope.parentCalls(node).filter(isCallbackCarrierCall)
    val viaMethodRef =
      JavaAnchorUsagesScope.firstAncestorOfType[Method](node).toList.flatMap { method =>
        val methodFullName = Option(method.fullName).getOrElse("")
        if (methodFullName.isEmpty) Nil
        else {
          cpg.methodRef
            .filter(ref => Option(ref.methodFullName).contains(methodFullName))
            .l
            .flatMap(ref => JavaAnchorUsagesScope.parentCalls(ref).filter(isCallbackCarrierCall))
        }
      }

    (direct ++ viaMethodRef).distinctBy(_.id)
  }

  private def callbackCapture(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] =
    callbackCarrierCalls(node).map(call => usageFromCall(call, Some(UsageKind.Listener), Some(anchor), Some(node)))

  private def directUsage(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] =
    if (anchor.usageType != "DIRECT_USAGE") Nil
    else node match {
      case call: Call if Option(call.name).contains("<operator>.assignment") =>
        List(usageFromCall(call, Some(UsageKind.Setter), Some(anchor), Some(node)))
      case call: Call if !isIgnored(call) =>
        List(usageFromCall(call, anchor = Some(anchor), target = Some(node)))
      case call: Call =>
        JavaAnchorUsagesScope.parentCalls(call)
          .filterNot(isIgnored)
          .take(3)
          .map(parent => usageFromCall(parent, anchor = Some(anchor), target = Some(node)))
      case _ =>
        JavaAnchorUsagesScope.parentCalls(node)
          .filterNot(isIgnored)
          .take(3)
          .map(parent => usageFromCall(parent, anchor = Some(anchor), target = Some(node)))
    }

  private def receiverUsage(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] =
    if (anchor.usageType == "DIRECT_USAGE") Nil
    else if (anchor.usageType == "RETURN") {
      node match {
        case call: Call => List(usageFromCall(call, Some(UsageKind.Delegate), Some(anchor), Some(node)))
        case _ => Nil
      }
    }
    else node match {
      case call: Call if !isIgnored(call) =>
        List(usageFromCall(call, anchor = Some(anchor), target = Some(node)))
      case call: Call =>
        JavaAnchorUsagesScope.parentCalls(call)
          .filterNot(isIgnored)
          .take(3)
          .map(parent => usageFromCall(parent, anchor = Some(anchor), target = Some(node)))
      case _ =>
        JavaAnchorUsagesScope.parentCalls(node)
          .filterNot(isIgnored)
          .take(3)
          .map(parent => usageFromCall(parent, anchor = Some(anchor), target = Some(node)))
    }

  private def anchorNameAppears(anchor: ViewAnchor, node: AstNode): Boolean = {
    val code = Option(node.code).getOrElse("")
    anchor.anchorName.exists(name => name.nonEmpty && code.contains(name))
  }

  private def isStructuralOperator(call: Call): Boolean =
    Option(call.name).exists(_.startsWith("<operator>."))

  private def isAnchorCreationCall(anchor: ViewAnchor, node: AstNode, call: Call): Boolean =
    call.id == node.id ||
      call.id == anchor.cpgNodeId ||
      anchor.occurrenceNodeId.contains(call.id)

  private def isResourceLookupCall(call: Call): Boolean = {
    val name = Option(call.name).getOrElse("").toLowerCase
    name.contains("findviewbyid") ||
      name.contains("requireviewbyid") ||
      name.contains("findfragmentbyid") ||
      name == "finditem"
  }

  private def callReadsFromAnchor(anchor: ViewAnchor, call: Call): Boolean =
    anchor.anchorName.exists { name =>
      Option(call.code).exists(_.contains(s"$name."))
    }

  private def containsTargetNode(argument: Expression, node: AstNode): Boolean =
    argument.id == node.id || argument.start.ast.exists(_.id == node.id)

  private def argumentPassesAnchorValue(anchor: ViewAnchor, argument: Expression): Boolean =
    anchor.anchorName.exists { name =>
      val code = Option(argument.code).getOrElse("")
      code.contains(name) && !code.contains(s"$name.")
    }

  private def isNonReceiverArgument(anchor: ViewAnchor, call: Call, node: AstNode): Boolean =
    call.astChildren
      .collectAll[Expression]
      .filter(argument => argument.argumentIndex > 0)
      .exists(argument => containsTargetNode(argument, node) && argumentPassesAnchorValue(anchor, argument))

  private def isPassThroughDelegateCall(anchor: ViewAnchor, node: AstNode, call: Call): Boolean =
    !isAnchorCreationCall(anchor, node, call) &&
      !isIgnored(call) &&
      !isStructuralOperator(call) &&
      !isResourceLookupCall(call) &&
      !callReadsFromAnchor(anchor, call) &&
      JavaAnchorUsagesUiSignals.classify(call) == UsageKind.Other &&
      isNonReceiverArgument(anchor, call, node)

  private def passThroughDelegate(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] =
    if (!anchorNameAppears(anchor, node)) Nil
    else {
      JavaAnchorUsagesScope.parentCalls(node)
        .filter(call => isPassThroughDelegateCall(anchor, node, call))
        .take(1)
        .flatMap { call =>
          val delegate = List(usageFromCall(call, Some(UsageKind.Delegate), Some(anchor), Some(node)))
          val calleeUsages =
            JavaAnchorUsagesScope.interProceduralArgumentTargets(node, call)
              .flatMap(calleeNode => receiverUsage(anchor, calleeNode) ++ callbackCapture(anchor, calleeNode))
          delegate ++ calleeUsages
        }
    }

  def detectSemanticUsage(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val usages =
      if (anchor.usageType == "DIRECT_USAGE") directUsage(anchor, node) ++ callbackCapture(anchor, node)
      else callbackCapture(anchor, node) ++ receiverUsage(anchor, node) ++ passThroughDelegate(anchor, node)

    usages
      .filter(usage => Option(usage.code).exists(_.trim.nonEmpty))
      .distinctBy(usage => (usage.nodeId, usage.usageKind.outputLabel, usage.methodFullName, usage.code))
  }
}
