object JavaViewAnchorJson {
  import JavaViewAnchorModel.*
  import JavaViewAnchorModel.JsonId.writeId

  private def opt(value: Option[String]): ujson.Value =
    value.map(ujson.Str.apply).getOrElse(ujson.Null)

  private def optId(value: Option[Long]): ujson.Value =
    value.map(writeId).getOrElse(ujson.Null)

  private def optInt(value: Option[Int]): ujson.Value =
    value.map(ujson.Num(_)).getOrElse(ujson.Null)

  def legacyInputAnchorToJson(anchor: JavaViewAnchor): ujson.Value =
    ujson.Obj(
      "view_type" -> anchor.viewType,
      "resource_id" -> anchor.resourceId,
      "usage_type" -> anchor.usageType,
      "cpg_node_id" -> writeId(anchor.cpgNodeId),
      "cpg_node_type" -> anchor.cpgNodeType,
      "anchor_name" -> anchor.anchorName.map(ujson.Str(_)).getOrElse(ujson.Null),
      "location" -> anchor.location,
      "code" -> anchor.code,
      "declaration_scope" -> anchor.declarationScope.map(ujson.Str(_)).getOrElse(ujson.Null)
    )

  def toJson(anchor: JavaViewAnchor): ujson.Value =
    legacyInputAnchorToJson(anchor)

  def viewInstanceToJson(instance: JavaViewInstance): ujson.Value =
    ujson.Obj(
      "occurrence_role" -> instance.occurrenceRole,
      "resource_id" -> instance.resourceId,
      "resource_owner" -> opt(instance.resourceOwner),
      "layout_context" -> opt(instance.layoutContext),
      "menu_context" -> opt(instance.menuContext),
      "binding_class" -> opt(instance.bindingClass),
      "binding_field" -> opt(instance.bindingField),
      "view_type" -> instance.viewType,
      "handle_name" -> opt(instance.handleName),
      "handle_owner_node_id" -> optId(instance.handleOwnerNodeId),
      "handle_owner_node_type" -> opt(instance.handleOwnerNodeType),
      "parent_node_id" -> optId(instance.parentNodeId),
      "parent_node_type" -> opt(instance.parentNodeType),
      "parent_call_name" -> opt(instance.parentCallName),
      "parent_location" -> opt(instance.parentLocation),
      "parent_code" -> opt(instance.parentCode),
      "argument_index" -> optInt(instance.argumentIndex),
      "enclosing_method_full_name" -> opt(instance.enclosingMethodFullName),
      "cpg_node_id" -> writeId(instance.cpgNodeId),
      "cpg_node_type" -> instance.cpgNodeType,
      "location" -> instance.location,
      "code" -> instance.code
    )

  private def legacyUsageType(instance: JavaViewInstance): String =
    if (instance.occurrenceRole == "USAGE") "DIRECT_USAGE"
    else if (instance.handleOwnerNodeId.exists(_ != instance.cpgNodeId)) "ASSIGNMENT"
    else "CHAINING"

  def legacyAnchorToJson(instance: JavaViewInstance): ujson.Value =
    ujson.Obj(
      "view_type" -> instance.viewType,
      "resource_id" -> instance.resourceId,
      "usage_type" -> legacyUsageType(instance),
      "cpg_node_id" -> writeId(instance.handleOwnerNodeId.getOrElse(instance.cpgNodeId)),
      "cpg_node_type" -> instance.handleOwnerNodeType.getOrElse(instance.cpgNodeType),
      "anchor_name" -> instance.handleName
        .orElse(instance.bindingField)
        .map(ujson.Str.apply)
        .getOrElse(ujson.Str(instance.code)),
      "location" -> instance.location,
      "code" -> instance.code,
      "declaration_scope" -> ujson.Null
    )

  def toJsonString(instances: List[JavaViewInstance]): String = {
    val sorted = instances.sortBy(instance =>
      (instance.location, instance.resourceId, instance.handleName.getOrElse(""), instance.cpgNodeId)
    )
    ujson.write(ujson.Arr(sorted.map(legacyAnchorToJson): _*), indent = 2)
  }

  def toV2JsonString(instances: List[JavaViewInstance]): String = {
    val sorted = instances.sortBy(instance =>
      (instance.location, instance.resourceId, instance.handleName.getOrElse(""), instance.cpgNodeId)
    )
    ujson.write(ujson.Arr(sorted.map(viewInstanceToJson): _*), indent = 2)
  }
}
