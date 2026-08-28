import io.shiftleft.codepropertygraph.Cpg

import JavaViewAnchorModel.JavaViewAnchor
import JavaViewAnchorModel.JsonId.readId

object JavaAssignmentDeclRefInput {
  def loadCpg(path: String): Cpg =
    io.joern.joerncli.console.Joern
      .importCpg(path)
      .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

  private def str(obj: collection.mutable.Map[String, ujson.Value], key: String): Option[String] =
    obj.get(key).flatMap(_.strOpt)

  private def int(obj: collection.mutable.Map[String, ujson.Value], key: String): Option[Int] =
    obj.get(key).flatMap(value => value.numOpt.map(_.toInt).orElse(value.strOpt.flatMap(_.toIntOption)))

  private def hasNonNullKey(obj: collection.mutable.Map[String, ujson.Value], key: String): Boolean =
    obj.get(key).exists(_ != ujson.Null)

  private def isV2ViewAnchor(obj: collection.mutable.Map[String, ujson.Value]): Boolean =
    hasNonNullKey(obj, "occurrence_role")

  private def v2UsageType(obj: collection.mutable.Map[String, ujson.Value]): String = {
    str(obj, "usage_type").getOrElse {
      val role = str(obj, "occurrence_role").getOrElse("")
      if (role == "USAGE") "DIRECT_USAGE"
      else if (str(obj, "parent_node_type").contains("RETURN")) "RETURN"
      else {
        val occurrenceNodeId = readId(obj, "cpg_node_id")
        val ownerNodeId = readId(obj, "handle_owner_node_id")
        if (ownerNodeId.exists(ownerId => occurrenceNodeId.forall(_ != ownerId))) "ASSIGNMENT"
        else "CHAINING"
      }
    }
  }

  private def v2CpgNodeId(obj: collection.mutable.Map[String, ujson.Value]): Long = {
    val occurrenceNodeId = readId(obj, "cpg_node_id").getOrElse(-1L)
    val role = str(obj, "occurrence_role").getOrElse("")
    if (role == "USAGE") occurrenceNodeId
    else readId(obj, "handle_owner_node_id").getOrElse(occurrenceNodeId)
  }

  private def v2CpgNodeType(obj: collection.mutable.Map[String, ujson.Value]): String = {
    val occurrenceNodeType = str(obj, "cpg_node_type").getOrElse("CALL")
    val role = str(obj, "occurrence_role").getOrElse("")
    if (role == "USAGE") occurrenceNodeType
    else str(obj, "handle_owner_node_type").getOrElse(occurrenceNodeType)
  }

  private def parseV2ViewAnchor(map: collection.mutable.Map[String, ujson.Value]): JavaViewAnchor =
    JavaViewAnchor(
      viewType = str(map, "view_type").getOrElse("UNKNOWN"),
      resourceId = str(map, "resource_id").getOrElse("UNKNOWN_RESOURCE"),
      usageType = v2UsageType(map),
      cpgNodeId = v2CpgNodeId(map),
      cpgNodeType = v2CpgNodeType(map),
      anchorName = str(map, "anchor_name").orElse(str(map, "handle_name")).orElse(str(map, "binding_field")),
      location = str(map, "location").getOrElse("?:-1"),
      code = str(map, "code").getOrElse(""),
      declarationScope = str(map, "declaration_scope"),
      occurrenceRole = str(map, "occurrence_role"),
      occurrenceNodeId = readId(map, "cpg_node_id"),
      occurrenceNodeType = str(map, "cpg_node_type"),
      occurrenceLocation = str(map, "location"),
      occurrenceCode = str(map, "code"),
      handleOwnerNodeId = readId(map, "handle_owner_node_id"),
      handleOwnerNodeType = str(map, "handle_owner_node_type"),
      parentNodeId = readId(map, "parent_node_id"),
      parentNodeType = str(map, "parent_node_type"),
      parentCallName = str(map, "parent_call_name"),
      parentLocation = str(map, "parent_location"),
      parentCode = str(map, "parent_code"),
      argumentIndex = int(map, "argument_index"),
      enclosingMethodFullName = str(map, "enclosing_method_full_name")
    )

  def parseViewAnchors(jsonPath: String): List[JavaViewAnchor] = {
    val source = scala.io.Source.fromFile(jsonPath)
    val content =
      try source.mkString
      finally source.close()

    ujson.read(content) match {
      case arr: ujson.Arr =>
        arr.value.toList.flatMap { value =>
          value.objOpt.flatMap { obj =>
            val map = obj.value
            if (isV2ViewAnchor(map)) {
              Some(parseV2ViewAnchor(map))
            } else {
            for {
              viewType <- str(map, "view_type").orElse(str(map, "viewType"))
              resourceId <- str(map, "resource_id").orElse(str(map, "resourceId"))
              usageType <- str(map, "usage_type").orElse(str(map, "usageType"))
              cpgNodeId <- readId(map, "cpg_node_id").orElse(readId(map, "anchor_node_id")).orElse(readId(map, "anchorNodeId"))
              location <- str(map, "location")
              code <- str(map, "code")
            } yield JavaViewAnchor(
              viewType = viewType,
              resourceId = resourceId,
              usageType = usageType,
              cpgNodeId = cpgNodeId,
              cpgNodeType = str(map, "cpg_node_type").orElse(str(map, "anchor_node_label")).getOrElse("CALL"),
              anchorName = str(map, "anchor_name").orElse(str(map, "targetName")),
              location = location,
              code = code,
              declarationScope = str(map, "declaration_scope")
            )
            }
          }
        }
      case _ => Nil
    }
  }
}
