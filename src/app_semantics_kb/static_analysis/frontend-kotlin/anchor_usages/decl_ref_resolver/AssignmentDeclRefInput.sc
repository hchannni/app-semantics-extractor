import io.shiftleft.codepropertygraph.Cpg

import ViewAnchorContract.ViewAnchor
import ViewAnchorContract.JsonId.readId

object AssignmentDeclRefInput {
  private def str(obj: collection.mutable.Map[String, ujson.Value], key: String): Option[String] =
    obj.get(key).flatMap(_.strOpt)

  private def long(obj: collection.mutable.Map[String, ujson.Value], key: String): Option[Long] =
    readId(obj, key)

  private def hasNonNullKey(obj: collection.mutable.Map[String, ujson.Value], key: String): Boolean =
    obj.get(key).exists(_ != ujson.Null)

  private def isV2ViewAnchor(obj: collection.mutable.Map[String, ujson.Value]): Boolean =
    hasNonNullKey(obj, "occurrence_role")

  private def v2UsageType(obj: collection.mutable.Map[String, ujson.Value]): String = {
    str(obj, "usage_type").getOrElse {
      val role = str(obj, "occurrence_role").getOrElse("")
      if (role == "USAGE") "DIRECT_USAGE"
      else {
        val occurrenceNodeId = long(obj, "cpg_node_id")
        val ownerNodeId = long(obj, "handle_owner_node_id")
        if (ownerNodeId.exists(ownerId => occurrenceNodeId.forall(_ != ownerId))) "ASSIGNMENT"
        else "CHAINING"
      }
    }
  }

  private def v2CpgNodeId(obj: collection.mutable.Map[String, ujson.Value]): Long = {
    val occurrenceNodeId = long(obj, "cpg_node_id").getOrElse(-1L)
    val role = str(obj, "occurrence_role").getOrElse("")
    if (role == "USAGE") occurrenceNodeId
    else long(obj, "handle_owner_node_id").getOrElse(occurrenceNodeId)
  }

  private def v2CpgNodeType(obj: collection.mutable.Map[String, ujson.Value]): String = {
    val occurrenceNodeType = str(obj, "cpg_node_type").getOrElse("CALL")
    val role = str(obj, "occurrence_role").getOrElse("")
    if (role == "USAGE") occurrenceNodeType
    else str(obj, "handle_owner_node_type").getOrElse(occurrenceNodeType)
  }

  private def parseV2ViewAnchor(obj: collection.mutable.Map[String, ujson.Value]): ViewAnchor =
    ViewAnchor(
      viewType = str(obj, "view_type").getOrElse("UNKNOWN"),
      resourceId = str(obj, "resource_id").getOrElse("UNKNOWN_RESOURCE"),
      usageType = v2UsageType(obj),
      cpgNodeId = v2CpgNodeId(obj),
      cpgNodeType = v2CpgNodeType(obj),
      anchorName = str(obj, "anchor_name").orElse(str(obj, "handle_name")).orElse(str(obj, "binding_field")),
      location = str(obj, "location").getOrElse("?:-1"),
      code = str(obj, "code").getOrElse(""),
      declarationScope = str(obj, "declaration_scope")
    )

  private def parseLegacyViewAnchor(obj: collection.mutable.Map[String, ujson.Value]): Option[ViewAnchor] =
    for {
      viewType <- str(obj, "view_type").orElse(str(obj, "viewType"))
      resourceId <- str(obj, "resource_id").orElse(str(obj, "resourceId"))
      usageType <- str(obj, "usage_type").orElse(str(obj, "usageType"))
      cpgNodeId <- long(obj, "cpg_node_id").orElse(long(obj, "anchor_node_id")).orElse(long(obj, "anchorNodeId"))
      location <- str(obj, "location")
      code <- str(obj, "code")
    } yield ViewAnchor(
      viewType = viewType,
      resourceId = resourceId,
      usageType = usageType,
      cpgNodeId = cpgNodeId,
      cpgNodeType = str(obj, "cpg_node_type").orElse(str(obj, "anchor_node_label")).getOrElse("CALL"),
      anchorName = str(obj, "anchor_name").orElse(str(obj, "targetName")),
      location = location,
      code = code,
      declarationScope = str(obj, "declaration_scope")
    )

  def loadCpg(path: String): Cpg =
    io.joern.joerncli.console.Joern
      .importCpg(path)
      .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

  def parseViewAnchors(jsonPath: String): List[ViewAnchor] = {
    val source = scala.io.Source.fromFile(jsonPath)
    val content =
      try source.mkString
      finally source.close()

    ujson.read(content) match {
      case arr: ujson.Arr =>
        arr.value.toList.flatMap { value =>
          value.objOpt.flatMap { obj =>
            if (isV2ViewAnchor(obj.value)) Some(parseV2ViewAnchor(obj.value))
            else parseLegacyViewAnchor(obj.value)
          }
        }
      case _ => Nil
    }
  }
}
