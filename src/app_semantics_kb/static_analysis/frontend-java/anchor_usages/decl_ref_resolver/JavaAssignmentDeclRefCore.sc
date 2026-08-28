import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.util.Try

import JavaViewAnchorModel.JavaViewAnchor

object JavaAssignmentDeclRefCore {
  def nodeById(nodeId: Long)(implicit cpg: Cpg): Option[AstNode] =
    Try(cpg.graph.node(nodeId)).toOption.collect { case node: AstNode => node }

  def nodeCode(node: AstNode): String =
    Option(node.code).map(_.trim).getOrElse("")

  def nodeLocation(node: AstNode): String = {
    val file = node.file.name.headOption.getOrElse("?")
    val line = node.lineNumber.getOrElse(-1)
    s"$file:$line"
  }

  def astParentOf(node: AstNode): Option[AstNode] =
    Try(node.astParent).toOption.collect { case ast: AstNode => ast }

  private def astAncestors(node: AstNode): List[AstNode] = {
    def loop(current: AstNode, acc: List[AstNode]): List[AstNode] =
      astParentOf(current) match {
        case Some(parent) => loop(parent, parent :: acc)
        case None => acc.reverse
      }
    loop(node, Nil)
  }

  def enclosingMethodFullName(node: AstNode)(implicit cpg: Cpg): String =
    astAncestors(node)
      .collectFirst { case method: Method => method }
      .flatMap(method => Option(method.fullName))
      .getOrElse("")

  def typeDeclOf(node: AstNode)(implicit cpg: Cpg): Option[TypeDecl] =
    astAncestors(node).collectFirst { case typeDecl: TypeDecl => typeDecl }

  private def rootName(raw: String): String =
    Option(raw).getOrElse("")
      .trim
      .takeWhile(ch => ch.isLetterOrDigit || ch == '_' || ch == '$')

  private def fieldAccessName(call: Call): Option[String] =
    Option(call.name).filter(_ == "<operator>.fieldAccess").flatMap { _ =>
      call.argument.collectFirst { case field: FieldIdentifier =>
        Option(field.canonicalName).orElse(Option(field.code))
      }.flatten.orElse {
        Option(call.code).map(_.split('.').lastOption.getOrElse("")).filter(_.nonEmpty)
      }
    }

  private def nameFromTarget(node: AstNode, fallback: Option[String]): Option[String] =
    node match {
      case id: Identifier => Option(id.name)
      case field: FieldIdentifier => Option(field.canonicalName).orElse(Option(field.code))
      case call: Call if Option(call.name).contains("<operator>.indexAccess") =>
        call.argument.collect { case ast: AstNode => ast }.headOption
          .flatMap(nameFromTarget(_, fallback))
          .orElse(Option(call.code).map(rootName).filter(_.nonEmpty))
          .orElse(fallback.map(rootName).filter(_.nonEmpty))
      case call: Call =>
        fieldAccessName(call)
          .orElse(Option(call.code).map(rootName).filter(_.nonEmpty))
          .orElse(fallback.map(rootName).filter(_.nonEmpty))
      case other => Option(other.code).map(rootName).filter(_.nonEmpty).orElse(fallback.map(rootName).filter(_.nonEmpty))
    }

  def searchDeclarationsIn(scope: AstNode, name: String): Option[Declaration] = {
    val direct = scope.astChildren.collect {
      case decl: Declaration if Option(decl.name).contains(name) => decl
    }.headOption

    direct.orElse {
      scope match {
        case method: Method =>
          method.parameter.collect {
            case param: MethodParameterIn if Option(param.name).contains(name) => param
          }.headOption
        case typeDecl: TypeDecl =>
          typeDecl.member.nameExact(name).headOption
        case _ => None
      }
    }
  }

  def resolveDeclarationFrom(node: AstNode, expectedName: Option[String])(implicit cpg: Cpg): Option[Declaration] = {
    val targetName = nameFromTarget(node, expectedName).getOrElse("")
    if (targetName.isEmpty) return None

    val direct = node match {
      case id: Identifier => id.refsTo.collect { case decl: Declaration => decl }.headOption
      case _ => None
    }

    def loop(current: AstNode): Option[Declaration] = {
      astParentOf(current).flatMap(parent => searchDeclarationsIn(parent, targetName).orElse(loop(parent)))
    }

    direct.orElse(loop(node))
  }

  def assignmentTargetFromAnchor(anchor: JavaViewAnchor)(implicit cpg: Cpg): Option[AstNode] =
    nodeById(anchor.cpgNodeId).flatMap {
      case node: Identifier => Some(node)
      case node: FieldIdentifier => Some(node)
      case call: Call if Option(call.name).contains("<operator>.assignment") =>
        call.argument(1).collect { case ast: AstNode => ast }.headOption
      case call: Call => Some(call)
      case node => Some(node)
    }

  def resolveDeclarationForAnchor(anchor: JavaViewAnchor)(implicit cpg: Cpg): Option[(AstNode, Declaration)] = {
    if (anchor.usageType != "ASSIGNMENT") return None
    assignmentTargetFromAnchor(anchor).flatMap { target =>
      resolveDeclarationFrom(target, anchor.anchorName).map(decl => target -> decl)
    }
  }
}
