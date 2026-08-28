object JavaViewAnchorModel {
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

  case class JavaViewInstance(
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

  case class JavaViewAnchor(
    viewType: String,
    resourceId: String,
    usageType: String,
    cpgNodeId: Long,
    cpgNodeType: String,
    anchorName: Option[String],
    location: String,
    code: String,
    declarationScope: Option[String] = None,
    occurrenceRole: Option[String] = None,
    occurrenceNodeId: Option[Long] = None,
    occurrenceNodeType: Option[String] = None,
    occurrenceLocation: Option[String] = None,
    occurrenceCode: Option[String] = None,
    handleOwnerNodeId: Option[Long] = None,
    handleOwnerNodeType: Option[String] = None,
    parentNodeId: Option[Long] = None,
    parentNodeType: Option[String] = None,
    parentCallName: Option[String] = None,
    parentLocation: Option[String] = None,
    parentCode: Option[String] = None,
    argumentIndex: Option[Int] = None,
    enclosingMethodFullName: Option[String] = None
  )

  object JsonId {
    private def parseLongString(value: String): Option[Long] =
      scala.util.Try(value.toLong).toOption

    def readLong(value: ujson.Value): Option[Long] =
      value.numOpt.map(_.toLong).orElse(value.strOpt.flatMap(parseLongString))

    def readId(obj: scala.collection.Map[String, ujson.Value], key: String): Option[Long] =
      obj.get(key).flatMap(readLong)

    def writeId(id: Long): ujson.Value =
      ujson.Str(id.toString)
  }
}
