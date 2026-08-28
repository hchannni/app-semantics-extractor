import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.annotation.tailrec

object ViewAnchorUsageAnalyzer {
  case class UsageAnalysis(
    usageType: String,
    targetNode: Option[AstNode],
    declarationScope: Option[String]
  )

  private val scopeFunctionNames = Set("apply", "run", "also", "let", "with")
  private val wrapperOperators = Set("<operator>.cast", "<operator>.not", "<operator>.expressionList")

  private def isAssignment(call: Call): Boolean =
    Option(call.name).contains("<operator>.assignment")

  private def isScopeFunction(call: Call): Boolean =
    scopeFunctionNames.contains(Option(call.name).getOrElse("").toLowerCase)

  @tailrec
  private def effectiveParent(node: AstNode): Option[AstNode] = {
    val parent = node.astParent.collect { case astNode: AstNode => astNode }.headOption
    parent match {
      case Some(call: Call) if wrapperOperators.contains(call.name) => effectiveParent(call)
      case other => other
    }
  }

  def analyzeDeclarationContext(target: AstNode)(implicit cpg: Cpg): Option[String] = target match {
    case _: FieldIdentifier =>
      Some("MEMBER")

    case id: Identifier =>
      val directDecl = id.refsTo.collect { case decl: Declaration => decl }.headOption

      directDecl.map {
        case _: Member => "MEMBER"
        case _: Local => "LOCAL"
        case _: MethodParameterIn => "PARAMETER"
        case _ => "UNKNOWN"
      }.orElse {
        val name = Option(id.name).getOrElse("")

        def findDeclInParents(node: AstNode): Option[String] = {
          val parentOpt = node.astParent.collect { case a: AstNode => a }.headOption

          parentOpt.flatMap { parent =>
            val declOpt = parent match {
              case typeDecl: TypeDecl =>
                typeDecl.member.nameExact(name).headOption.map(_ => "MEMBER")
              case method: Method =>
                method.local.nameExact(name).headOption.map(_ => "LOCAL")
                  .orElse(method.parameter.nameExact(name).headOption.map(_ => "PARAMETER"))
              case block: Block =>
                block.astChildren.collect { case local: Local if Option(local.name).contains(name) => local }
                  .headOption.map(_ => "LOCAL")
              case _ => None
            }

            declOpt.orElse(findDeclInParents(parent))
          }
        }

        if (name.nonEmpty) findDeclInParents(id) else None
      }

    case call: Call if Option(call.name).contains("<operator>.fieldAccess") =>
      Some("MEMBER")

    case _ => None
  }

  def analyzeUsage(call: Call)(implicit cpg: Cpg): UsageAnalysis = {
    val parent = effectiveParent(call)

    parent match {
      case Some(assign: Call) if isAssignment(assign) =>
        val target = assign.argument(1).collect { case node: AstNode => node }.headOption
        val declarationScope = target.flatMap(analyzeDeclarationContext)
        UsageAnalysis("ASSIGNMENT", target, declarationScope)

      case Some(scopeCall: Call) if isScopeFunction(scopeCall) =>
        UsageAnalysis("SCOPE", Some(scopeCall), declarationScope = None)

      case Some(parentCall: Call) if !isAssignment(parentCall) =>
        UsageAnalysis("CHAINING", Some(parentCall), declarationScope = None)

      case Some(_: Return) =>
        UsageAnalysis("RETURN", Some(call), declarationScope = None)

      case _ =>
        UsageAnalysis("ORPHAN", None, declarationScope = None)
    }
  }
}
