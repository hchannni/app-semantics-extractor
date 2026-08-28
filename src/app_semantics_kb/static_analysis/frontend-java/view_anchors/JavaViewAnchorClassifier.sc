import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

import scala.annotation.tailrec
import scala.util.Try

object JavaViewAnchorClassifier {
  import JavaViewAnchorModel.*

  case class HandleOwner(
    name: String,
    nodeId: Long,
    nodeType: String,
    viewType: Option[String] = None
  )

  private val wrapperOperators = Set(
    "<operator>.cast",
    "<operator>.expressionList",
    "<operator>.not",
    "<operator>.logicalNot",
    "<operator>.ref"
  )

  private val castPattern = """\(([\w\.$]+)\)\s*.*findViewById""".r

  def astParentOf(node: AstNode): Option[AstNode] =
    Try(node.astParent).toOption.collect { case astNode: AstNode => astNode }

  def locationOf(node: AstNode): String =
    Try(s"${node.location.filename}:${node.lineNumber.getOrElse(-1)}")
      .getOrElse(s"?:${node.lineNumber.getOrElse(-1)}")

  private def nodeCode(node: AstNode): String =
    Option(node.code).getOrElse("")

  private def parentCallName(node: AstNode): Option[String] =
    astParentOf(node).collect { case call: Call => Option(call.name).getOrElse("") }.filter(_.nonEmpty)

  private def argumentIndex(node: AstNode): Option[Int] =
    node match {
      case expression: Expression => Try(expression.argumentIndex).toOption.filter(_ >= 0)
      case _ => None
    }

  private def enclosingMethodFullName(node: AstNode): Option[String] =
    node.start.repeat(_.astParent)(_.emit).collectAll[Method].headOption.flatMap { method =>
      Option(method.fullName).filter(_.nonEmpty)
    }

  @tailrec
  private def effectiveOwnerParent(node: AstNode): Option[AstNode] = {
    val parent = astParentOf(node)
    parent match {
      case Some(call: Call) if wrapperOperators.contains(call.name) => effectiveOwnerParent(call)
      case other => other
    }
  }

  private def assignmentTarget(assign: Call): Option[AstNode] =
    assign.argument(1).collect { case node: AstNode => node }.headOption

  private def fieldNameFromFieldAccess(call: Call): Option[String] =
    Option(call.name).filter(_ == "<operator>.fieldAccess").flatMap { _ =>
      call.argument.collectFirst { case field: FieldIdentifier =>
        Option(field.canonicalName).orElse(Option(field.code))
      }.flatten.orElse {
        Option(call.code).map(_.split('.').lastOption.getOrElse("")).filter(_.nonEmpty)
      }
    }

  private def viewTypeFromNode(node: AstNode): Option[String] =
    node match {
      case id: Identifier =>
        Option(id.typeFullName).filter(isMeaningfulType).map(_.split('.').last)
      case call: Call =>
        Option(call.typeFullName).filter(isMeaningfulType).map(_.split('.').last)
      case _ => None
    }

  private def isMeaningfulType(typeFullName: String): Boolean = {
    val value = Option(typeFullName).getOrElse("")
    value.nonEmpty && value != "ANY" && value != "void"
  }

  private def ownerFromTarget(target: AstNode): Option[HandleOwner] = {
    val name = target match {
      case id: Identifier =>
        Option(id.name)
      case field: FieldIdentifier =>
        Option(field.canonicalName).orElse(Option(field.code))
      case call: Call if Option(call.code).exists(_.startsWith("this.")) =>
        fieldNameFromFieldAccess(call)
      case call: Call =>
        fieldNameFromFieldAccess(call).orElse(Option(call.code))
      case _ =>
        Option(target.code).map(_.trim).filter(_.nonEmpty)
    }

    name.map { resolvedName =>
      HandleOwner(
        name = resolvedName,
        nodeId = target.id,
        nodeType = target.label,
        viewType = viewTypeFromNode(target)
      )
    }
  }

  def ownerFromAssignment(assign: Call): Option[HandleOwner] =
    assignmentTarget(assign).flatMap(ownerFromTarget)

  @tailrec
  def assignmentOwnerFromParents(node: AstNode): Option[HandleOwner] =
    effectiveOwnerParent(node) match {
      case Some(assign: Call) if Option(assign.name).contains("<operator>.assignment") =>
        ownerFromAssignment(assign)
      case Some(parent: AstNode) =>
        assignmentOwnerFromParents(parent)
      case None =>
        None
    }

  def resourceHandleOwner(call: Call): Option[HandleOwner] =
    if (Option(call.name).contains("<operator>.assignment")) ownerFromAssignment(call)
    else assignmentOwnerFromParents(call)

  def syntheticOwner(call: Call, decl: ResourceViewDecl): HandleOwner =
    HandleOwner(
      name = decl.resourceName,
      nodeId = call.id,
      nodeType = call.label
    )

  def bindingOwner(
    call: Call,
    field: String,
    viewType: Option[String] = None
  ): HandleOwner =
    HandleOwner(
      name = field,
      nodeId = call.id,
      nodeType = call.label,
      viewType = viewType
    )

  private def viewTypeFromCast(code: String): Option[String] =
    castPattern.findFirstMatchIn(code).map(_.group(1).split('.').lastOption.getOrElse("UNKNOWN"))

  private def viewTypeFromNearestCast(call: Call): Option[String] =
    astParentOf(call).collect {
      case cast: Call if Option(cast.name).contains("<operator>.cast") =>
        cast.argument.collectFirst {
          case typeRef: TypeRef => Option(typeRef.code).orElse(Option(typeRef.typeFullName))
        }.flatten
    }.flatten.map(_.split('.').last).filter(_.nonEmpty)

  private def generatedTypeFor(
    decl: ResourceViewDecl,
    fieldTypes: Map[(String, String), ViewBindingFieldType]
  ): Option[String] =
    for {
      bindingClass <- decl.bindingClass
      bindingField <- decl.bindingField
      generatedType <- fieldTypes.get((bindingClass, bindingField))
    } yield generatedType.fieldType

  def mkInstance(
    call: Call,
    decl: ResourceViewDecl,
    fieldTypes: Map[(String, String), ViewBindingFieldType],
    occurrenceRole: String = "HANDLE",
    handleOwner: Option[HandleOwner] = None
  ): JavaViewInstance = {
    val parent = astParentOf(call)
    val viewType =
      generatedTypeFor(decl, fieldTypes)
        .orElse(viewTypeFromCast(Option(call.code).getOrElse("")))
        .orElse(viewTypeFromNearestCast(call))
        .orElse(handleOwner.flatMap(_.viewType))
        .getOrElse(JavaViewInstanceRules.declViewType(decl))

    JavaViewInstance(
      occurrenceRole = occurrenceRole,
      resourceId = decl.resourceId,
      resourceOwner = decl.resourceOwner,
      layoutContext = decl.layoutName,
      menuContext = decl.menuName,
      bindingClass = decl.bindingClass,
      bindingField = decl.bindingField,
      viewType = viewType,
      handleName = handleOwner.map(_.name),
      handleOwnerNodeId = handleOwner.map(_.nodeId),
      handleOwnerNodeType = handleOwner.map(_.nodeType),
      parentNodeId = parent.map(_.id),
      parentNodeType = parent.map(_.label),
      parentCallName = parentCallName(call),
      parentLocation = parent.map(locationOf),
      parentCode = parent.map(nodeCode),
      argumentIndex = argumentIndex(call),
      enclosingMethodFullName = enclosingMethodFullName(call),
      cpgNodeId = call.id,
      cpgNodeType = call.label,
      location = locationOf(call),
      code = Option(call.code).getOrElse("")
    )
  }
}
