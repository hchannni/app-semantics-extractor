object ViewAnchorV2Contract {
  case class ResourceViewDecl(
    resourceId: String,
    resourceName: String,
    resourceKind: String,
    sourceType: String,
    sourceFile: String,
    sourcePath: String,
    layoutName: Option[String],
    menuName: Option[String],
    xmlTag: String,
    bindingClass: Option[String],
    bindingField: Option[String],
    idDeclKind: String,
    line: Option[Int],
    resourceOwner: Option[String] = None,
    qualifiedResourceId: Option[String] = None,
    sourceOrigin: String = "app_xml",
    dependencyCoordinate: Option[String] = None,
    qualifiers: Option[String] = None
  )

  case class ViewBindingFieldType(
    bindingClass: String,
    bindingClassFullName: String,
    fieldName: String,
    fieldType: String,
    fieldTypeRaw: String,
    sourceFile: String,
    sourcePath: String,
    line: Option[Int],
    resolution: String,
    evidence: List[String]
  )

  case class ViewInstance(
    occurrenceRole: String,
    resourceId: String,
    resourceOwner: Option[String],
    layoutContext: Option[String],
    menuContext: Option[String],
    bindingClass: Option[String],
    bindingField: Option[String],
    viewType: String,
    handleName: Option[String],
    handleOwnerNodeId: Option[Long],
    handleOwnerNodeType: Option[String],
    parentNodeId: Option[Long],
    parentNodeType: Option[String],
    parentCallName: Option[String],
    parentLocation: Option[String],
    parentCode: Option[String],
    argumentIndex: Option[Int],
    enclosingMethodFullName: Option[String],
    cpgNodeId: Long,
    cpgNodeType: String,
    location: String,
    code: String
  )

  object JsonId {
    def writeId(id: Long): ujson.Value =
      ujson.Str(id.toString)
  }
}
