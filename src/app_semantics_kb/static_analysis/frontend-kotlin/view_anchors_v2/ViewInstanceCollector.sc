import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.annotation.tailrec
import scala.util.Try

import ViewAnchorV2Contract.{ResourceViewDecl, ViewBindingFieldType, ViewInstance}

object ViewInstanceCollector {
  private case class HandleOwner(
    name: String,
    nodeId: Long,
    nodeType: String
  )

  private case class InventoryIndex(
    byResourceId: Map[String, List[ResourceViewDecl]],
    byResourceName: Map[String, List[ResourceViewDecl]],
    byBindingField: Map[String, List[ResourceViewDecl]],
    byBindingClass: Map[String, List[ResourceViewDecl]]
  )

  private case class BindingFieldTypeIndex(
    byClassAndField: Map[(String, String), ViewBindingFieldType],
    byField: Map[String, List[ViewBindingFieldType]]
  ) {
    def lookup(decl: ResourceViewDecl): Option[ViewBindingFieldType] =
      for {
        bindingClass <- decl.bindingClass
        bindingField <- decl.bindingField
        generatedType <- byClassAndField.get((bindingClass, bindingField))
      } yield generatedType

    def lookup(bindingClass: String, field: String): Option[ViewBindingFieldType] =
      byClassAndField.get((bindingClass, field))

    def bindingClassesFor(field: String): Set[String] =
      byField.getOrElse(field, Nil).map(_.bindingClass).toSet
  }

  private def buildIndex(decls: List[ResourceViewDecl]): InventoryIndex =
    InventoryIndex(
      byResourceId = decls
        .flatMap(decl => (List(decl.resourceId) ++ decl.qualifiedResourceId.toList).distinct.map(_ -> decl))
        .groupMap(_._1)(_._2),
      byResourceName = decls.groupBy(_.resourceName),
      byBindingField = decls.flatMap(decl => decl.bindingField.map(_ -> decl)).groupMap(_._1)(_._2),
      byBindingClass = decls.flatMap(decl => decl.bindingClass.map(_ -> decl)).groupMap(_._1)(_._2)
    )

  private def buildBindingFieldTypeIndex(fields: List[ViewBindingFieldType]): BindingFieldTypeIndex =
    BindingFieldTypeIndex(
      byClassAndField = fields.map(field => (field.bindingClass, field.fieldName) -> field).toMap,
      byField = fields.groupBy(_.fieldName)
    )

  private def location(call: Call): String =
    s"${call.location.filename}:${call.lineNumber.getOrElse(-1)}"

  private def location(node: AstNode): String =
    Try(s"${node.location.filename}:${node.lineNumber.getOrElse(-1)}")
      .getOrElse(s"?:${node.lineNumber.getOrElse(-1)}")

  private def filename(call: Call): Option[String] =
    Try(call.location.filename).toOption.map(_.trim).filter(_.nonEmpty)

  private def nodeCode(node: AstNode): String =
    Option(node.code).getOrElse("")

  private def simpleBindingClassName(typeFullName: String): Option[String] =
    Option(typeFullName)
      .map(_.trim)
      .filter(_.nonEmpty)
      .map(_.replaceAll("[!?]+$", ""))
      .map(_.split("[.$]").lastOption.getOrElse(""))
      .filter(_.endsWith("Binding"))

  private def nodeTypeFullName(node: AstNode): Option[String] =
    (node match {
      case id: Identifier => Try(id.typeFullName).toOption
      case call: Call => Try(call.typeFullName).toOption
      case local: Local => Try(local.typeFullName).toOption
      case member: Member => Try(member.typeFullName).toOption
      case param: MethodParameterIn => Try(param.typeFullName).toOption
      case typeRef: TypeRef => Try(typeRef.typeFullName).toOption
      case _ => None
    }).map(_.trim).filter(_.nonEmpty)

  private def bindingClassHint(call: Call): Option[String] = {
    val directArgumentHint =
      call.argument
        .collect { case node: AstNode => node }
        .flatMap(node => nodeTypeFullName(node).flatMap(simpleBindingClassName))
        .headOption

    directArgumentHint.orElse {
      Try {
        call.ast
          .collect { case id: Identifier => id }
          .l
          .flatMap(id => nodeTypeFullName(id).flatMap(simpleBindingClassName))
          .headOption
      }.toOption.flatten
    }
  }

  private def parentCallName(node: AstNode): Option[String] =
    astParentOf(node).collect { case call: Call => Option(call.name).getOrElse("") }.filter(_.nonEmpty)

  private def argumentIndex(node: AstNode): Option[Int] =
    node match {
      case expression: Expression => Try(expression.argumentIndex).toOption.filter(_ >= 0)
      case _ => None
    }

  private def enclosingMethodFullName(node: AstNode): Option[String] =
    enclosingMethod(node).flatMap(method => Option(method.fullName).filter(_.nonEmpty))

  private def astParentOf(node: AstNode): Option[AstNode] =
    Try(node.astParent).toOption.collect { case astNode: AstNode => astNode }

  private val ownerWrapperOperators = Set(
    "<operator>.cast",
    "<operator>.expressionList",
    "<operator>.not",
    "<operator>.logicalNot",
    "<operator>.ref"
  )

  @tailrec
  private def effectiveOwnerParent(node: AstNode): Option[AstNode] = {
    val parent = astParentOf(node)
    parent match {
      case Some(call: Call) if ownerWrapperOperators.contains(call.name) => effectiveOwnerParent(call)
      case other => other
    }
  }

  private def assignmentTarget(assign: Call): Option[AstNode] =
    assign.argument(1).collect { case node: AstNode => node }.headOption

  private def fieldNameFromFieldAccess(call: Call): Option[String] =
    Option(call.name).filter(_ == "<operator>.fieldAccess").flatMap { _ =>
      call.argument.collectFirst { case field: FieldIdentifier =>
        Option(field.canonicalName).orElse(Option(field.code))
      }.flatten
    }

  private def ownerFromTarget(target: AstNode): Option[HandleOwner] = {
    val name = target match {
      case id: Identifier =>
        Option(id.name)
      case field: FieldIdentifier =>
        Option(field.canonicalName).orElse(Option(field.code))
      case call: Call if Option(call.code).exists(_.startsWith("this.")) =>
        fieldNameFromFieldAccess(call)
      case _: Call =>
        None
      case _ =>
        Option(target.code).map(_.trim).filter(_.nonEmpty)
    }

    name.map { resolvedName =>
      HandleOwner(
        name = resolvedName,
        nodeId = target.id,
        nodeType = target.label
      )
    }
  }

  private def ownerFromAssignment(assign: Call): Option[HandleOwner] =
    assignmentTarget(assign).flatMap(ownerFromTarget)

  @tailrec
  private def assignmentOwnerFromParents(node: AstNode): Option[HandleOwner] =
    effectiveOwnerParent(node) match {
      case Some(assign: Call) if Option(assign.name).contains("<operator>.assignment") =>
        ownerFromAssignment(assign)
      case Some(parent: AstNode) =>
        assignmentOwnerFromParents(parent)
      case None =>
        None
    }

  private def enclosingMethod(node: AstNode): Option[Method] =
    node.start.repeat(_.astParent)(_.emit).collectAll[Method].headOption

  private def lambdaMethodRef(method: Method)(implicit cpg: Cpg): Option[MethodRef] =
    Option(method.fullName)
      .filter(fullName => fullName.contains(".<lambda>") || fullName.contains(".<anonymous>"))
      .flatMap(fullName => cpg.methodRef.filter(ref => Option(ref.methodFullName).contains(fullName)).headOption)

  private def lambdaOwnerFromMethodRef(node: AstNode)(implicit cpg: Cpg): Option[HandleOwner] =
    enclosingMethod(node)
      .flatMap(lambdaMethodRef)
      .flatMap(ref => assignmentOwnerFromParents(ref))

  private def resourceHandleOwner(call: Call)(implicit cpg: Cpg): Option[HandleOwner] =
    if (Option(call.name).contains("<operator>.assignment")) ownerFromAssignment(call)
    else assignmentOwnerFromParents(call).orElse(lambdaOwnerFromMethodRef(call))

  private def syntheticOwner(
    call: Call,
    decl: ResourceViewDecl
  ): HandleOwner =
    HandleOwner(
      name = decl.resourceName,
      nodeId = call.id,
      nodeType = call.label
    )

  private def bindingOwner(
    call: Call,
    field: String
  ): HandleOwner =
    HandleOwner(
      name = field,
      nodeId = call.id,
      nodeType = call.label
    )

  private def mkInstance(
    call: Call,
    decl: ResourceViewDecl,
    fieldTypes: BindingFieldTypeIndex,
    occurrenceRole: String = "HANDLE",
    handleOwner: Option[HandleOwner] = None
  ): ViewInstance = {
    val generatedType = fieldTypes.lookup(decl)
    val parent = astParentOf(call)
    ViewInstance(
      occurrenceRole = occurrenceRole,
      resourceId = decl.resourceId,
      resourceOwner = decl.resourceOwner,
      layoutContext = decl.layoutName,
      menuContext = decl.menuName,
      bindingClass = decl.bindingClass,
      bindingField = decl.bindingField,
      viewType = generatedType.map(_.fieldType).getOrElse(ViewInstanceRules.declViewType(decl)),
      handleName = handleOwner.map(_.name),
      handleOwnerNodeId = handleOwner.map(_.nodeId),
      handleOwnerNodeType = handleOwner.map(_.nodeType),
      parentNodeId = parent.map(_.id),
      parentNodeType = parent.map(_.label),
      parentCallName = parentCallName(call),
      parentLocation = parent.map(location),
      parentCode = parent.map(nodeCode),
      argumentIndex = argumentIndex(call),
      enclosingMethodFullName = enclosingMethodFullName(call),
      cpgNodeId = call.id,
      cpgNodeType = call.label,
      location = location(call),
      code = Option(call.code).getOrElse("")
    )
  }

  private def collectResourceCalls(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex
  )(implicit cpg: Cpg): List[ViewInstance] =
    cpg.call
      .filter { call =>
        val code = Option(call.code).getOrElse("")
        code.contains("R.id.") &&
          !ViewInstanceRules.isPropertyAccessAroundResourceLookup(call.name, code) &&
          (ViewInstanceRules.isResourceLookupAnchorCall(call.name, code) ||
            ViewInstanceRules.isNavigationResourceTargetCall(call.name, code) ||
            ViewInstanceRules.isMenuItemLookupName(call.name))
      }
      .flatMap { call =>
        val code = Option(call.code).getOrElse("")
        val requiresSyntheticOwner =
          ViewInstanceRules.isMenuItemLookupName(call.name) ||
            ViewInstanceRules.isNavigationResourceTargetCall(call.name, code)
        ViewInstanceRules.resourceRefsIn(code).flatMap { ref =>
          val matches = resourceMatches(index, ref)
          matches.flatMap { decl =>
            val resolvedResourceOwner = resourceHandleOwner(call)
            val owner =
              if (requiresSyntheticOwner) Some(syntheticOwner(call, decl))
              else if (Option(call.name).contains("<operator>.assignment") && resolvedResourceOwner.isEmpty) None
              else resolvedResourceOwner.orElse(Some(syntheticOwner(call, decl)))
            owner.toList.map { resolvedOwner =>
              mkInstance(call, decl, fieldTypes, handleOwner = Some(resolvedOwner))
            }
          }
        }
      }
      .l

  private def collectResourceInteractionCalls(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex
  )(implicit cpg: Cpg): List[ViewInstance] =
    cpg.call
      .filter { call =>
        val code = Option(call.code).getOrElse("")
        code.contains("R.id.") &&
          ViewInstanceRules.isDirectInteractionCall(call.name) &&
          !ViewInstanceRules.isResourceLookupAnchorCall(call.name, code) &&
          !ViewInstanceRules.isNavigationResourceTargetCall(call.name, code)
      }
      .flatMap { call =>
        val code = Option(call.code).getOrElse("")
        ViewInstanceRules.resourceRefsIn(code).flatMap { ref =>
          val matches = resourceMatches(index, ref)
          matches.map(decl =>
            mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(syntheticOwner(call, decl))
            )
          )
        }
      }
      .l

  private def resourceMatches(index: InventoryIndex, ref: ViewInstanceRules.ResourceRef): List[ResourceViewDecl] = {
    val exact = index.byResourceId.getOrElse(ref.raw, Nil)
    if (exact.nonEmpty) exact
    else ref.owner match {
      case Some(owner) =>
        index.byResourceId.getOrElse(s"$owner.R.id.${ref.name}", Nil)
      case None =>
        index.byResourceName.getOrElse(ref.name, Nil)
    }
  }

  private def collectDirectBindingFields(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex
  )(implicit cpg: Cpg): List[ViewInstance] =
    cpg.call
      .name("<operator>.fieldAccess")
      .filter(call => ViewInstanceRules.exactBindingFieldAccess(call.code).nonEmpty)
      .flatMap { call =>
        ViewInstanceRules.exactBindingFieldAccess(call.code).toList.flatMap { access =>
          val matches = bindingFieldMatches(
            index,
            fieldTypes,
            access.field,
            bindingClassHint(call),
            filename(call)
          )
          matches.map(decl =>
            mkInstance(
              call,
              decl,
              fieldTypes,
              handleOwner = Some(bindingOwner(call, access.field))
            )
          )
        }
      }
      .l

  private def collectDirectBindingInteractions(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex
  )(implicit cpg: Cpg): List[ViewInstance] =
    cpg.call
      .filter(call => ViewInstanceRules.isInteractionCall(call.name, call.code))
      .filter(call => ViewInstanceRules.anyBindingFieldsIn(call.code).nonEmpty)
      .flatMap { call =>
        ViewInstanceRules.anyBindingFieldsIn(call.code).flatMap { field =>
          val matches = bindingFieldMatches(
            index,
            fieldTypes,
            field,
            bindingClassHint(call),
            filename(call)
          )
          matches.map(decl =>
            mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(bindingOwner(call, field))
            )
          )
        }
      }
      .l

  private def collectBindingScopeInteractions(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex
  )(implicit cpg: Cpg): List[ViewInstance] =
    cpg.call
      .filter(call => ViewInstanceRules.isInteractionCall(call.name, call.code))
      .flatMap { call =>
        val receiver = ViewInstanceRules.receiverName(call.code)
        receiver.toList.flatMap { field =>
          val matches = bindingFieldMatches(
            index,
            fieldTypes,
            field,
            bindingClassHint(call),
            filename(call)
          )
          matches.map(decl =>
            mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(bindingOwner(call, field))
            )
          )
        }
      }
      .l

  private def bindingFieldMatches(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex,
    field: String,
    bindingClassHint: Option[String] = None,
    sourceFilename: Option[String] = None
  ): List[ResourceViewDecl] = {
    val candidates = index.byBindingField
      .getOrElse(field, Nil)
      .filter { decl =>
        decl.sourceOrigin == "app_xml" || fieldTypes.lookup(decl).nonEmpty
      }

    val classScoped = bindingClassHint
      .map(simpleName)
      .map { hintedClass =>
        candidates.filter { decl =>
          decl.bindingClass.exists(bindingClass => simpleName(bindingClass) == hintedClass) ||
            fieldTypes.lookup(hintedClass, field).exists(generated => decl.bindingClass.contains(generated.bindingClass))
        }
      }
      .getOrElse(Nil)

    val narrowed = if (classScoped.nonEmpty) classScoped else candidates
    val sourceScoped = sourceFilename
      .map(name => narrowed.filter(decl => ViewInstanceRules.sameSourceAffinity(name, decl)))
      .getOrElse(Nil)

    if (classScoped.isEmpty && sourceScoped.nonEmpty) sourceScoped else narrowed
  }

  private def simpleName(name: String): String =
    Option(name).getOrElse("").split("[.$]").lastOption.getOrElse("")

  private def collectBindingInflates(
    index: InventoryIndex,
    fieldTypes: BindingFieldTypeIndex
  )(implicit cpg: Cpg): List[ViewInstance] =
    cpg.call
      .filter(call => ViewInstanceRules.bindingClassesIn(call.code).nonEmpty)
      .flatMap { call =>
        ViewInstanceRules.bindingClassesIn(call.code).flatMap { bindingClass =>
          val matches = index.byBindingClass.getOrElse(bindingClass, Nil)
          matches.take(1).map { decl =>
            mkInstance(
              call,
              decl,
              fieldTypes,
              handleOwner = Some(syntheticOwner(call, decl))
            )
          }
        }
      }
      .l

  private def dedupe(instances: List[ViewInstance]): List[ViewInstance] =
    instances
      .groupBy(instance => (
        instance.resourceId,
        instance.location,
        instance.code,
        instance.handleName,
        instance.handleOwnerNodeId
      ))
      .values
      .flatMap(_.sortBy(_.cpgNodeId).headOption)
      .toList
      .sortBy(instance => (instance.location, instance.resourceId, instance.cpgNodeId))

  def collect(
    decls: List[ResourceViewDecl],
    generatedFieldTypes: List[ViewBindingFieldType] = Nil
  )(implicit cpg: Cpg): List[ViewInstance] = {
    val index = buildIndex(decls)
    val fieldTypes = buildBindingFieldTypeIndex(generatedFieldTypes)
    dedupe(
        collectResourceCalls(index, fieldTypes) ++
        collectResourceInteractionCalls(index, fieldTypes) ++
        collectDirectBindingFields(index, fieldTypes) ++
        collectDirectBindingInteractions(index, fieldTypes) ++
        collectBindingScopeInteractions(index, fieldTypes) ++
        collectBindingInflates(index, fieldTypes)
    )
  }
}
