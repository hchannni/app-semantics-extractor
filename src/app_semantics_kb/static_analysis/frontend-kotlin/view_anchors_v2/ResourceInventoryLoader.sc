import java.nio.file.{Files, Paths}

import ViewAnchorV2Contract.ResourceViewDecl

object ResourceInventoryLoader {
  private def optString(obj: ujson.Obj, key: String): Option[String] =
    obj.value.get(key).flatMap {
      case ujson.Str(value) if value.nonEmpty => Some(value)
      case _ => None
    }

  private def reqString(obj: ujson.Obj, key: String): String =
    optString(obj, key).getOrElse("")

  private def optInt(obj: ujson.Obj, key: String): Option[Int] =
    obj.value.get(key).flatMap {
      case ujson.Num(value) => Some(value.toInt)
      case ujson.Str(value) => scala.util.Try(value.toInt).toOption
      case _ => None
    }

  private def parseDecl(value: ujson.Value): ResourceViewDecl = {
    val obj = value.obj
    ResourceViewDecl(
      resourceId = reqString(obj, "resource_id"),
      resourceName = reqString(obj, "resource_name"),
      resourceKind = reqString(obj, "resource_kind"),
      sourceType = reqString(obj, "source_type"),
      sourceFile = reqString(obj, "source_file"),
      sourcePath = reqString(obj, "source_path"),
      layoutName = optString(obj, "layout_name"),
      menuName = optString(obj, "menu_name"),
      xmlTag = reqString(obj, "xml_tag"),
      bindingClass = optString(obj, "binding_class"),
      bindingField = optString(obj, "binding_field"),
      idDeclKind = reqString(obj, "id_decl_kind"),
      line = optInt(obj, "line"),
      resourceOwner = optString(obj, "resource_owner"),
      qualifiedResourceId = optString(obj, "qualified_resource_id"),
      sourceOrigin = optString(obj, "source_origin").getOrElse("app_xml"),
      dependencyCoordinate = optString(obj, "dependency_coordinate"),
      qualifiers = optString(obj, "qualifiers")
    )
  }

  def load(path: String): List[ResourceViewDecl] = {
    val text = Files.readString(Paths.get(path))
    val json = ujson.read(text)
    json("declarations").arr.map(parseDecl).toList
  }
}
