import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

object JavaViewAnchorCollector {
  import JavaViewAnchorModel.*
  import JavaViewAnchorClassifier.HandleOwner

  private case class InventoryIndex(
    byResourceId: Map[String, List[ResourceViewDecl]],
    byResourceName: Map[String, List[ResourceViewDecl]],
    byBindingField: Map[String, List[ResourceViewDecl]],
    byBindingClass: Map[String, List[ResourceViewDecl]]
  )

  private def buildIndex(decls: List[ResourceViewDecl]): InventoryIndex =
    InventoryIndex(
      byResourceId = decls
        .flatMap(decl => (List(decl.resourceId) ++ decl.qualifiedResourceId.toList).distinct.map(_ -> decl))
        .groupMap(_._1)(_._2),
      byResourceName = decls.groupBy(_.resourceName),
      byBindingField = decls.flatMap(decl => decl.bindingField.map(_ -> decl)).groupMap(_._1)(_._2),
      byBindingClass = decls.flatMap(decl => decl.bindingClass.map(_ -> decl)).groupMap(_._1)(_._2)
    )

  private def buildFieldTypeIndex(fields: List[ViewBindingFieldType]): Map[(String, String), ViewBindingFieldType] =
    fields.map(field => (field.bindingClass, field.fieldName) -> field).toMap

  private def isJavaSource(node: AstNode): Boolean =
    Option(node.filename).exists(_.endsWith(".java"))

  private def fallbackDecl(ref: JavaViewInstanceRules.ResourceRef): ResourceViewDecl =
    ResourceViewDecl(
      resourceId = ref.raw,
      resourceName = ref.name,
      resourceKind = "unknown",
      sourceType = "unknown",
      sourceFile = "",
      sourcePath = "",
      layoutName = None,
      menuName = None,
      xmlTag = "UNKNOWN",
      bindingClass = None,
      bindingField = None,
      idDeclKind = "",
      line = None,
      resourceOwner = ref.owner,
      qualifiedResourceId = Some(ref.raw),
      sourceOrigin = "cpg_code"
    )

  private def resourceMatches(
    index: InventoryIndex,
    ref: JavaViewInstanceRules.ResourceRef
  ): List[ResourceViewDecl] = {
    if (ref.raw == JavaViewInstanceRules.DynamicResourceId) {
      return List(fallbackDecl(ref))
    }
    val exact = index.byResourceId.getOrElse(ref.raw, Nil)
    if (exact.nonEmpty) exact
    else {
      val ownerMatches = ref.owner.toList.flatMap { owner =>
        index.byResourceId.getOrElse(s"$owner.R.id.${ref.name}", Nil)
      }
      if (ownerMatches.nonEmpty) ownerMatches
      else {
        val byName = index.byResourceName.getOrElse(ref.name, Nil)
        if (byName.nonEmpty) byName else List(fallbackDecl(ref))
      }
    }
  }

  private def collectResourceAliases()(implicit cpg: Cpg): Map[String, List[JavaViewInstanceRules.ResourceRef]] = {
    val entries = cpg.call
      .filter(isJavaSource)
      .flatMap(call => JavaViewInstanceRules.resourceAliasesIn(call.code).toList)
      .l

    entries
      .groupMap(_._1)(_._2)
      .view
      .mapValues(_.flatten.distinct)
      .toMap
  }

  private def bindingFieldMatches(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType],
    field: String
  ): List[ResourceViewDecl] =
    index.byBindingField
      .getOrElse(field, Nil)
      .filter { decl =>
        decl.sourceOrigin == "app_xml" ||
          (for {
            bindingClass <- decl.bindingClass
            bindingField <- decl.bindingField
          } yield fieldTypes.contains((bindingClass, bindingField))).getOrElse(false)
      }

  private def bindingFieldType(
    fieldTypes: Map[(String, String), ViewBindingFieldType],
    decl: ResourceViewDecl
  ): Option[String] =
    for {
      bindingClass <- decl.bindingClass
      bindingField <- decl.bindingField
      fieldType <- fieldTypes.get((bindingClass, bindingField))
    } yield fieldType.fieldType

  private def isLayoutCompatibleDecl(decl: ResourceViewDecl): Boolean = {
    val sourceType = decl.sourceType.toLowerCase
    val resourceKind = decl.resourceKind.toLowerCase
    sourceType != "menu" && !resourceKind.contains("menu")
  }

  private def collectResourceCalls(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType],
    aliases: Map[String, List[JavaViewInstanceRules.ResourceRef]]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .filter(isJavaSource)
      .filter { call =>
        val code = Option(call.code).getOrElse("")
        val refs = JavaViewInstanceRules.resourceRefsOrAliasesIn(code, aliases)
        val dynamicRefs = JavaViewInstanceRules.dynamicResourceRefsIn(call.name, code, aliases)
        (refs.nonEmpty || dynamicRefs.nonEmpty) &&
          !JavaViewInstanceRules.isPropertyAccessAroundResourceLookup(call.name, code) &&
          (JavaViewInstanceRules.isResourceLookupAnchorCall(call.name, code) ||
            JavaViewInstanceRules.isNavigationResourceTargetCall(call.name, code) ||
            JavaViewInstanceRules.isMenuItemLookupName(call.name))
      }
      .flatMap { call =>
        val code = Option(call.code).getOrElse("")
        val requiresSyntheticOwner =
            JavaViewInstanceRules.isMenuItemLookupName(call.name) ||
            JavaViewInstanceRules.isNavigationResourceTargetCall(call.name, code)

        val refs = JavaViewInstanceRules.resourceRefsOrAliasesIn(code, aliases)
        val effectiveRefs =
          if (refs.nonEmpty) refs
          else JavaViewInstanceRules.dynamicResourceRefsIn(call.name, code, aliases)

        effectiveRefs.flatMap { ref =>
          resourceMatches(index, ref).flatMap { decl =>
            val resolvedOwner = JavaViewAnchorClassifier.resourceHandleOwner(call)
            val owner =
              if (requiresSyntheticOwner) Some(JavaViewAnchorClassifier.syntheticOwner(call, decl))
              else if (Option(call.name).contains("<operator>.assignment") && resolvedOwner.isEmpty) None
              else resolvedOwner.orElse(Some(JavaViewAnchorClassifier.syntheticOwner(call, decl)))

            owner.toList.map { resolved =>
              JavaViewAnchorClassifier.mkInstance(call, decl, fieldTypes, handleOwner = Some(resolved))
            }
          }
        }
      }
      .l

  private def collectResourceInteractionCalls(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType],
    aliases: Map[String, List[JavaViewInstanceRules.ResourceRef]]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .filter(isJavaSource)
      .filter { call =>
        val code = Option(call.code).getOrElse("")
        JavaViewInstanceRules.resourceRefsOrAliasesIn(code, aliases).nonEmpty &&
          JavaViewInstanceRules.isDirectInteractionCall(call.name) &&
          !JavaViewInstanceRules.isResourceLookupAnchorCall(call.name, code) &&
          !JavaViewInstanceRules.isNavigationResourceTargetCall(call.name, code)
      }
      .flatMap { call =>
        val code = Option(call.code).getOrElse("")
        JavaViewInstanceRules.resourceRefsOrAliasesIn(code, aliases).flatMap { ref =>
          resourceMatches(index, ref).map { decl =>
            JavaViewAnchorClassifier.mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(JavaViewAnchorClassifier.syntheticOwner(call, decl))
            )
          }
        }
      }
      .l

  private def collectHelperReturnedViewInteractions(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType],
    aliases: Map[String, List[JavaViewInstanceRules.ResourceRef]]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .filter(isJavaSource)
      .filter { call =>
        JavaViewInstanceRules.isViewReceiverUsageCall(call.name)
      }
      .flatMap { call =>
        val helperCalls = call.receiver
          .collectAll[Call]
          .filter(helper => helper.id != call.id)
          .filter(helper => JavaViewInstanceRules.isAndroidViewLikeType(helper.typeFullName))
          .filter(helper => JavaViewInstanceRules.resourceRefsOrAliasesIn(helper.code, aliases).nonEmpty)
          .l

        helperCalls.flatMap { helper =>
          JavaViewInstanceRules.resourceRefsOrAliasesIn(helper.code, aliases).flatMap { ref =>
            resourceMatches(index, ref)
              .filter(isLayoutCompatibleDecl)
              .map { decl =>
                JavaViewAnchorClassifier.mkInstance(
                  call,
                  decl,
                  fieldTypes,
                  occurrenceRole = "USAGE",
                  handleOwner = Some(JavaViewAnchorClassifier.syntheticOwner(call, decl))
                )
              }
          }
        }
      }
      .l

  private def collectDirectBindingFields(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .name("<operator>.fieldAccess")
      .filter(isJavaSource)
      .filter(call => JavaViewInstanceRules.directBindingFieldsIn(call.code).nonEmpty)
      .flatMap { call =>
        JavaViewInstanceRules.directBindingFieldsIn(call.code).flatMap { field =>
          bindingFieldMatches(index, fieldTypes, field).map { decl =>
            JavaViewAnchorClassifier.mkInstance(
              call,
              decl,
              fieldTypes,
              handleOwner = Some(JavaViewAnchorClassifier.bindingOwner(call, field, bindingFieldType(fieldTypes, decl)))
            )
          }
        }
      }
      .l

  private def collectBindingAssignments(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .name("<operator>.assignment")
      .filter(isJavaSource)
      .filter(call => JavaViewInstanceRules.bindingFieldsOnAssignmentTarget(call.code).nonEmpty)
      .flatMap { call =>
        JavaViewInstanceRules.bindingFieldsOnAssignmentTarget(call.code).flatMap { field =>
          bindingFieldMatches(index, fieldTypes, field).map { decl =>
            JavaViewAnchorClassifier.mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(JavaViewAnchorClassifier.bindingOwner(call, field, bindingFieldType(fieldTypes, decl)))
            )
          }
        }
      }
      .l

  private def collectDirectBindingInteractions(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .filter(isJavaSource)
      .filter(call => JavaViewInstanceRules.isInteractionCall(call.name, call.code))
      .filter(call => JavaViewInstanceRules.anyBindingFieldsIn(call.code).nonEmpty)
      .flatMap { call =>
        JavaViewInstanceRules.anyBindingFieldsIn(call.code).flatMap { field =>
          bindingFieldMatches(index, fieldTypes, field).map { decl =>
            JavaViewAnchorClassifier.mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(JavaViewAnchorClassifier.bindingOwner(call, field, bindingFieldType(fieldTypes, decl)))
            )
          }
        }
      }
      .l

  private def collectBindingReceiverInteractions(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .filter(isJavaSource)
      .filter(call => JavaViewInstanceRules.isInteractionCall(call.name, call.code))
      .flatMap { call =>
        JavaViewInstanceRules.receiverName(call.code).toList.flatMap { field =>
          bindingFieldMatches(index, fieldTypes, field).map { decl =>
            JavaViewAnchorClassifier.mkInstance(
              call,
              decl,
              fieldTypes,
              occurrenceRole = "USAGE",
              handleOwner = Some(JavaViewAnchorClassifier.bindingOwner(call, field, bindingFieldType(fieldTypes, decl)))
            )
          }
        }
      }
      .l

  private def collectBindingInflates(
    index: InventoryIndex,
    fieldTypes: Map[(String, String), ViewBindingFieldType]
  )(implicit cpg: Cpg): List[JavaViewInstance] =
    cpg.call
      .filter(isJavaSource)
      .filter(call => JavaViewInstanceRules.bindingClassesIn(call.code).nonEmpty)
      .flatMap { call =>
        JavaViewInstanceRules.bindingClassesIn(call.code).flatMap { bindingClass =>
          index.byBindingClass.getOrElse(bindingClass, Nil).take(1).map { decl =>
            JavaViewAnchorClassifier.mkInstance(
              call,
              decl,
              fieldTypes,
              handleOwner = Some(JavaViewAnchorClassifier.syntheticOwner(call, decl))
            )
          }
        }
      }
      .l

  private def dedupe(instances: List[JavaViewInstance]): List[JavaViewInstance] =
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
    decls: List[ResourceViewDecl] = Nil,
    generatedFieldTypes: List[ViewBindingFieldType] = Nil
  )(implicit cpg: Cpg): List[JavaViewInstance] = {
    val index = buildIndex(decls)
    val fieldTypes = buildFieldTypeIndex(generatedFieldTypes)
    val aliases = collectResourceAliases()
    dedupe(
        collectResourceCalls(index, fieldTypes, aliases) ++
        collectResourceInteractionCalls(index, fieldTypes, aliases) ++
        collectHelperReturnedViewInteractions(index, fieldTypes, aliases) ++
        collectDirectBindingFields(index, fieldTypes) ++
        collectBindingAssignments(index, fieldTypes) ++
        collectDirectBindingInteractions(index, fieldTypes) ++
        collectBindingReceiverInteractions(index, fieldTypes) ++
        collectBindingInflates(index, fieldTypes)
    )
  }
}
