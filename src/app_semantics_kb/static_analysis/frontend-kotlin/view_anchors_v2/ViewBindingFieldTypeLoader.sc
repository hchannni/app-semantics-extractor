import java.nio.file.{Files, Paths}

import ViewAnchorV2Contract.ViewBindingFieldType

object ViewBindingFieldTypeLoader {
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

  private def strList(obj: ujson.Obj, key: String): List[String] =
    obj.value.get(key).toList.flatMap {
      case arr: ujson.Arr => arr.value.collect { case ujson.Str(value) => value }.toList
      case _ => Nil
    }

  private def parseField(value: ujson.Value): ViewBindingFieldType = {
    val obj = value.obj
    ViewBindingFieldType(
      bindingClass = reqString(obj, "binding_class"),
      bindingClassFullName = reqString(obj, "binding_class_full_name"),
      fieldName = reqString(obj, "field_name"),
      fieldType = reqString(obj, "field_type"),
      fieldTypeRaw = reqString(obj, "field_type_raw"),
      sourceFile = reqString(obj, "source_file"),
      sourcePath = reqString(obj, "source_path"),
      line = optInt(obj, "line"),
      resolution = reqString(obj, "resolution"),
      evidence = strList(obj, "evidence")
    )
  }

  def load(path: String): List[ViewBindingFieldType] = {
    val trimmed = Option(path).getOrElse("").trim
    if (trimmed.isEmpty) return Nil
    val input = Paths.get(trimmed)
    if (!Files.exists(input)) return Nil

    val json = ujson.read(Files.readString(input))
    json("fields").arr.map(parseField).toList
  }
}
