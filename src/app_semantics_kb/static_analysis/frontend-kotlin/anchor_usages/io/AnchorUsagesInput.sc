import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*

import scala.util.Try

// Reads pipeline input JSON files and resolves CPG node references.
// Separates JSON parsing + CPG binding from the serialization concerns in AnchorUsagesJson.
object AnchorUsagesInput {
  import AnchorUsagesModel.*
  import ViewAnchorContract.JsonId.readId

  private def strOpt(obj: collection.mutable.Map[String, ujson.Value], key: String): Option[String] =
    obj.get(key).flatMap(_.strOpt)

  private def arrValues(obj: collection.mutable.Map[String, ujson.Value], key: String): List[ujson.Value] =
    obj.get(key).map(_.arr.toList).getOrElse(Nil)

  private def hasNonNullKey(obj: collection.mutable.Map[String, ujson.Value], key: String): Boolean =
    obj.get(key).exists(_ != ujson.Null)

  private def isV2ViewAnchor(obj: collection.mutable.Map[String, ujson.Value]): Boolean =
    hasNonNullKey(obj, "occurrence_role")

  private def v2UsageType(obj: collection.mutable.Map[String, ujson.Value]): String = {
    strOpt(obj, "usage_type").getOrElse {
      val role = strOpt(obj, "occurrence_role").getOrElse("")
      if (role == "USAGE") "DIRECT_USAGE"
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
    val role = strOpt(obj, "occurrence_role").getOrElse("")
    if (role == "USAGE") occurrenceNodeId
    else readId(obj, "handle_owner_node_id").getOrElse(occurrenceNodeId)
  }

  private def v2CpgNodeType(obj: collection.mutable.Map[String, ujson.Value]): String = {
    val occurrenceNodeType = strOpt(obj, "cpg_node_type").getOrElse("CALL")
    val role = strOpt(obj, "occurrence_role").getOrElse("")
    if (role == "USAGE") occurrenceNodeType
    else strOpt(obj, "handle_owner_node_type").getOrElse(occurrenceNodeType)
  }

  private def parseV2ViewAnchor(obj: collection.mutable.Map[String, ujson.Value]): ViewAnchor =
    ViewAnchor(
      cpgNodeId    = v2CpgNodeId(obj),
      cpgNodeType  = v2CpgNodeType(obj),
      usageType    = v2UsageType(obj),
      resourceId   = strOpt(obj, "resource_id").getOrElse("UNKNOWN_RESOURCE"),
      viewType     = strOpt(obj, "view_type").getOrElse("UNKNOWN"),
      anchorName   = strOpt(obj, "anchor_name").orElse(strOpt(obj, "handle_name")).orElse(strOpt(obj, "binding_field")),
      location     = strOpt(obj, "location").getOrElse("?:-1"),
      code         = strOpt(obj, "code").getOrElse(""),
      declarationScope = strOpt(obj, "declaration_scope")
    )

  private def parseViewAnchor(
    obj: collection.mutable.Map[String, ujson.Value],
    defaultUsageType: String
  ): ViewAnchor =
    if (isV2ViewAnchor(obj)) parseV2ViewAnchor(obj)
    else {
      ViewAnchor(
        cpgNodeId    = readId(obj, "cpg_node_id").orElse(readId(obj, "anchor_node_id")).getOrElse(-1L),
        cpgNodeType  = strOpt(obj, "cpg_node_type").orElse(strOpt(obj, "anchor_node_label")).getOrElse("CALL"),
        usageType    = strOpt(obj, "usage_type").getOrElse(defaultUsageType),
        resourceId   = strOpt(obj, "resource_id").getOrElse("UNKNOWN_RESOURCE"),
        viewType     = strOpt(obj, "view_type").getOrElse("UNKNOWN"),
        anchorName   = strOpt(obj, "anchor_name"),
        location     = strOpt(obj, "location").getOrElse("?:-1"),
        code         = strOpt(obj, "code").getOrElse(""),
        declarationScope = strOpt(obj, "declaration_scope")
      )
    }

  def parseAnchors(jsonPath: String): List[ViewAnchor] = {
    val json = AnchorUsagesJson.readJson(jsonPath)
    json.arr.toList.flatMap { value =>
      value.objOpt.map(obj => parseViewAnchor(obj.value, defaultUsageType = "UNKNOWN"))
    }
  }

  def parseDeclarations(jsonPath: String)(implicit cpg: Cpg): Map[Long, AnchorDeclaration] = {
    val json = AnchorUsagesJson.readJson(jsonPath)
    val parsedEntries = json.arr.toList.flatMap { anchorValue =>
      anchorValue.objOpt.map { obj =>
        val anchorObj = obj.value.get("anchor").flatMap(_.objOpt)
        val anchor    = anchorObj.map(ao => parseViewAnchor(ao.value, defaultUsageType = "UNKNOWN"))

        val declarations = arrValues(obj.value, "declarations").flatMap { declValue =>
          declValue.objOpt.toList.flatMap { declObj =>
            val nodeIdOpt = readId(declObj.value, "nodeId")

            val declarationNode = nodeIdOpt.flatMap(nodeId =>
              Try(cpg.graph.node(nodeId)).toOption.collect { case decl: Declaration => decl }
            )

            val refs = arrValues(declObj.value, "references")
              .flatMap { refValue =>
                refValue.objOpt.map { refObj =>
                  DeclarationReference(
                    nodeId        = readId(refObj.value, "nodeId").getOrElse(-1L),
                    nodeLabel     = refObj.value.get("nodeLabel").flatMap(_.strOpt).getOrElse(""),
                    code          = refObj.value.get("code").flatMap(_.strOpt).getOrElse(""),
                    location      = refObj.value.get("location").flatMap(_.strOpt).getOrElse(""),
                    methodFullName = refObj.value.get("methodFullName").flatMap(_.strOpt).getOrElse("")
                  )
                }
              }
              .distinctBy(_.nodeId)
              .sortBy(_.nodeId)

            declarationNode.map(_ -> refs).toList
          }
        }

        anchor.map(a => a.cpgNodeId -> AnchorDeclaration(a, declarations))
      }
    }.flatten

    parsedEntries
      .groupBy(_._1)
      .view
      .mapValues { grouped =>
        val anchor = grouped.head._2.anchor
        val mergedDeclarations = grouped
          .flatMap(_._2.declarations)
          .groupBy(_._1.id)
          .values
          .flatMap { sameDecl =>
            sameDecl.headOption.map { case (decl, _) =>
              val refs = sameDecl.flatMap(_._2).distinctBy(_.nodeId).sortBy(_.nodeId)
              decl -> refs
            }
          }
          .toList
          .sortBy(_._1.id)

        AnchorDeclaration(anchor, mergedDeclarations)
      }
      .toMap
  }
}
