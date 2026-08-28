import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

// Detects usages where the anchor is captured inside a lambda/closure passed to a call.
// Two paths are covered:
//   (A) MethodRef — explicit lambda reference attached to the call
//   (B) ClosureBinding — captured variable inside an inline block argument
object AnchorUsagesClosureCaptureCase {
  import AnchorUsagesModel.*
  import AnchorUsagesUiSignals.*

  // Remove local variable declarations from lambda body code so the output
  // focuses on the meaningful statements.
  private def filterLambdaDeclarations(lambdaBody: String): String = {
    val lines = lambdaBody.split("\n").map(_.trim).filter(_.nonEmpty)
    val filtered = lines.filterNot(AnchorUsagesCaseSupport.isFalsePositive)
    if (filtered.isEmpty) lambdaBody.trim else filtered.mkString("\n")
  }

  // Extract the code of an inline block argument (e.g. trailing lambda body).
  private def inlineLambdaBody(call: Call): Option[String] =
    (call.argument.collectAll[Block].l ++ call.astChildren.collectAll[Block].l)
      .distinctBy(_.id)
      .flatMap(block => Option(block.code).map(_.trim).filter(_.nonEmpty))
      .sortBy(code => -code.length)
      .headOption
      .map(filterLambdaDeclarations)

  // Collect CPG node IDs of declarations that could resolve to the anchor reference.
  private def candidateDeclarationIds(node: AstNode, anchor: ViewAnchor)(implicit cpg: Cpg): Set[Long] = {
    val direct = node match {
      case id: Identifier =>
        (id.refsTo.collectAll[Declaration].id.l ++ id.start._refOut.collectAll[Declaration].id.l).toSet
      case _ => Set.empty[Long]
    }
    if (direct.nonEmpty) return direct

    val nameHints = Set(
      anchor.anchorName.getOrElse(""),
      node match {
        case id: Identifier       => Option(id.name).getOrElse("")
        case fi: FieldIdentifier  => Option(fi.canonicalName).getOrElse("")
        case _                    => ""
      }
    ).filter(_.nonEmpty)

    val methodDecls = AnchorUsagesCaseSupport.firstAncestorOfType[Method](node).toList.flatMap { method =>
      nameHints.toList.flatMap { name =>
        method.local.nameExact(name).id.l ++ method.parameter.nameExact(name).id.l
      }
    }
    val typeDecls = AnchorUsagesCaseSupport.firstAncestorOfType[TypeDecl](node).toList.flatMap { typeDecl =>
      nameHints.toList.flatMap(name => typeDecl.member.nameExact(name).id.l)
    }
    (methodDecls ++ typeDecls).toSet
  }

  // Path (B): detect anchor capture via ClosureBinding inside an inline block.
  private def detectViaClosureBinding(
    anchor: ViewAnchor,
    node: AstNode,
    call: Call
  )(implicit cpg: Cpg): Option[SemanticUsage] = {
    val lambdaBody = inlineLambdaBody(call)
    if (lambdaBody.isEmpty) return None

    val declarationIds = candidateDeclarationIds(node, anchor)
    val nameHints = Set(
      anchor.anchorName.getOrElse(""),
      node match {
        case id: Identifier       => Option(id.name).getOrElse("")
        case fi: FieldIdentifier  => Option(fi.canonicalName).getOrElse("")
        case _                    => ""
      }
    ).filter(_.nonEmpty)

    val hasClosureEvidence = call.ast.collectAll[Identifier].l.exists { id =>
      val capturedDeclIds = id._capturedByIn
        .collectAll[ClosureBinding]
        .flatMap(_._refOut.collectAll[Declaration])
        .id.l.toSet

      val capturesAnchorDecl =
        declarationIds.nonEmpty && capturedDeclIds.exists(declarationIds.contains)
      val capturesByNameHint =
        declarationIds.isEmpty &&
          capturedDeclIds.nonEmpty &&
          nameHints.contains(Option(id.name).getOrElse(""))

      capturesAnchorDecl || capturesByNameHint
    }

    if (!hasClosureEvidence) return None

    classifyCallName(call.name).map { sig =>
      SemanticUsage(
        usageKind = usageKindOf(sig),
        nodeLabel = call.label,
        code = lambdaBody.get,
        sourceLocation = SourceLocation.fromFileLine(call.filename, call.lineNumber.getOrElse(-1)),
        methodFullName = call.method.methodFullName,
        nodeId = call.id,
        usageMethodFullName = call.method.methodFullName
      )
    }
  }

  // Entry point for a single (node, call) pair.
  // Path (A): MethodRef attached to the call → reads the referenced lambda method's body.
  // Falls back to Path (B) if no MethodRef found.
  def detectOnCall(anchor: ViewAnchor, node: AstNode, call: Call)(implicit cpg: Cpg): Option[SemanticUsage] = {
    if (anchor.usageType != "CHAINING" && !AnchorUsagesCaseSupport.isWithinReceiver(call, node)) return None

    val methodRefOpt =
      (call.argument.collectAll[MethodRef].l ++ call.astChildren.collectAll[MethodRef].l)
        .distinctBy(_.id)
        .headOption

    methodRefOpt.flatMap { methodRef =>
      val methodFullName = Option(methodRef.methodFullName).getOrElse("")
      if (methodFullName.isEmpty) return None

      cpg.method.fullNameExact(methodFullName).headOption.flatMap { lambdaMethod =>
        val rawLambdaBodyCode = Option(lambdaMethod.block).flatMap(b => Option(b.code)).getOrElse("")
        val cleanedLambdaBody = filterLambdaDeclarations(rawLambdaBodyCode)
        val lambdaMethodName = Option(lambdaMethod.fullName).filter(_.nonEmpty).getOrElse(methodFullName)

        classifyCallName(call.name).map { sig =>
          SemanticUsage(
            usageKind = usageKindOf(sig),
            nodeLabel = call.label,
            code = cleanedLambdaBody,
            sourceLocation = SourceLocation.fromFileLine(call.filename, call.lineNumber.getOrElse(-1)),
            methodFullName = call.method.methodFullName,
            nodeId = call.id,
            usageMethodFullName = lambdaMethodName
          )
        }
      }
    }.orElse(detectViaClosureBinding(anchor, node, call))
  }

  def detect(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val direct = node match {
      case call: Call => detectOnCall(anchor, node, call).toList
      case _          => Nil
    }

    if (direct.nonEmpty) direct
    else {
      AnchorUsagesCaseSupport.preferredCarrierCall(node)
        .flatMap(call => detectOnCall(anchor, node, call))
        .toList
    }
  }
}
