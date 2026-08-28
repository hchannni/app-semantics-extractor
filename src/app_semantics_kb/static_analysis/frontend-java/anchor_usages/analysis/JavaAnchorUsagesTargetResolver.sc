import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*

object JavaAnchorUsagesTargetResolver {
  import JavaAnchorUsagesModel.*

  def targetNodesFor(anchor: ViewAnchor, declarations: Option[AnchorDeclaration])(implicit cpg: Cpg): List[AstNode] =
    anchor.usageType match {
      case "ASSIGNMENT" =>
        declarations.toList
          .flatMap(_.declarations)
          .flatMap { case (_, refs) => refs.map(_.nodeId) }
          .flatMap(JavaAnchorUsagesScope.loadNode)
          .distinctBy(_.id)
      case "RETURN" =>
        JavaAnchorUsagesScope.loadNode(anchor.cpgNodeId)
          .toList
          .flatMap(JavaAnchorUsagesScope.callerCallSitesOf)
          .distinctBy(_.id)
      case _ =>
        JavaAnchorUsagesScope.loadNode(anchor.cpgNodeId).toList
    }
}
