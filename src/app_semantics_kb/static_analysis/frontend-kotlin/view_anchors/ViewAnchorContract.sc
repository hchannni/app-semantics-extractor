object ViewAnchorContract {
  case class ViewAnchor(
    viewType: String,
    resourceId: String,
    usageType: String,
    cpgNodeId: Long,
    cpgNodeType: String,
    anchorName: Option[String],
    location: String,
    code: String,
    declarationScope: Option[String] = None
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

    def writeOptId(idOpt: Option[Long]): ujson.Value =
      idOpt.map(writeId).getOrElse(ujson.Null)
  }
}
