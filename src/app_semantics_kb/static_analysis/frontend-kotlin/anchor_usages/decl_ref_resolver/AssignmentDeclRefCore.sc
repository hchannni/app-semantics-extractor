import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.util.Try

import ViewAnchorContract.ViewAnchor

object AssignmentDeclRefCore {
  def nodeById(nodeId: Long)(implicit cpg: Cpg): Option[AstNode] =
    Try(cpg.graph.node(nodeId))
      .toOption
      .collect { case node: AstNode => node }

  def nodeLocation(node: AstNode): String = {
    val file = node.file.name.headOption.getOrElse("?")
    val line = node.lineNumber.getOrElse(-1)
    s"$file:$line"
  }

  def nodeCode(node: AstNode): String =
    Option(node.code)
      .map(_.trim)
      .getOrElse("")

  def enclosingMethodFullName(node: AstNode)(implicit cpg: Cpg): String =
    node.start
      .repeat(_.astParent)(_.emit)
      .collect { case m: Method => m }
      .headOption
      .flatMap(m => Option(m.fullName))
      .getOrElse("")

  def typeDeclOf(node: AstNode)(implicit cpg: Cpg): Option[TypeDecl] =
    node.start.repeat(_.astParent)(_.emit).collectAll[TypeDecl].headOption

  /**
   * 현재 스코프의 로컬 선언(변수 등) -> 메서드 파라미터 순으로 가장 가까운 선언을 하나 찾아주는 유틸.
   */
  def searchDeclarationsIn(scope: AstNode, name: String): Option[Declaration] = {
    val matches =
      scope.astChildren.collect {
        case decl: Declaration if Option(decl.name).contains(name) => decl
      }.headOption

    matches.orElse {
      scope match {
        case method: Method =>
          method.parameter.collect {
            case param: MethodParameterIn if Option(param.name).contains(name) => param
          }.headOption
        case _ => None
      }
    }
  }

  def resolveDeclaration(identifier: Identifier, expectedName: Option[String])(implicit cpg: Cpg): Option[Declaration] = {
    val targetName = expectedName.orElse(Option(identifier.name)).getOrElse("")

    val direct =
      identifier.refsTo.collect { case decl: Declaration => decl }.headOption

    def loop(node: AstNode): Option[Declaration] = {
      val parentOpt = node.astParent.collect { case ast: AstNode => ast }.headOption
      parentOpt match {
        case None => None
        case Some(parent) =>
          searchDeclarationsIn(parent, targetName).orElse(loop(parent))
      }
    }

    direct.orElse(loop(identifier))
  }

  /**
   * anchor.cpgNodeId로 LHS Identifier를 복원한다.
   * ASSIGNMENT 케이스: cpgNodeId가 Identifier/FieldIdentifier를 직접 가리킨다.
   * 레거시/폴백: cpgNodeId가 Call이면 inAssignment.argument(1)에서 찾는다.
   */
  def assignmentIdentifierFromAnchor(anchor: ViewAnchor)(implicit cpg: Cpg): Option[Identifier] =
    nodeById(anchor.cpgNodeId).flatMap {
      case id: Identifier   => Some(id)
      case call: Call       =>
        call.inAssignment.argument(1).collect { case id: Identifier => id }.headOption
      case _                => None
    }

  def resolveDeclarationForAnchor(anchor: ViewAnchor)(implicit cpg: Cpg): Option[(Identifier, Declaration)] = {
    if (anchor.usageType != "ASSIGNMENT") return None

    assignmentIdentifierFromAnchor(anchor).flatMap { idNode =>
      resolveDeclaration(idNode, anchor.anchorName).map(decl => idNode -> decl)
    }
  }
}
