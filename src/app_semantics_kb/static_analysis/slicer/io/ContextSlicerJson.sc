import scala.util.Try
import java.nio.file.{Files, Paths}
import java.nio.charset.StandardCharsets

/** Context Slicer JSON 직렬화 / 역직렬화.
  *
  * 입력 파싱:
  *   parseAnchorUsages — anchor-usages.json → AnchorUsageInput[]
  *
  * 출력 (3파일 분리):
  *   writeOutput(outputDir, slices, globalMethodBodies, globalTypeIndex)
  *     → slices.json        : MethodSlice 메타데이터 + method_body_refs / domain_type_refs (코드 없음)
  *     → method-bodies.json : { fullName → MethodInfo } 전역 lookup (중복 제거)
  *     → type-index.json    : { typeFullName → TypeInfo } 전역 lookup (중복 제거)
  *
  * 설계 기준: docs/PROJECT_GUIDE.md 섹션 C-1~C-4
  */
object ContextSlicerJson {
  import ContextSlicerModel.*
  import ViewAnchorContract.JsonId.{readId, writeId}

  def readJson(path: String): ujson.Value = {
    val source = scala.io.Source.fromFile(path)
    try ujson.read(source.mkString)
    finally source.close()
  }

  private def str(obj: collection.mutable.Map[String, ujson.Value], key: String): String =
    obj.get(key).flatMap(_.strOpt).getOrElse("")

  private def strOpt(obj: collection.mutable.Map[String, ujson.Value], key: String): Option[String] =
    obj.get(key).flatMap(v => if (v == ujson.Null) None else v.strOpt)

  private def int(obj: collection.mutable.Map[String, ujson.Value], key: String): Int =
    obj.get(key).flatMap(_.numOpt).map(_.toInt)
      .orElse(obj.get(key).flatMap(_.strOpt).flatMap(v => Try(v.toInt).toOption))
      .getOrElse(-1)

  /** anchor-usages.json 파싱.
    *
    * 구조:
    *   [ { anchor: { resource_id, view_type, usage_type, declaration_scope, ... },
    *       usages: [ { anchor_id, usage_point: { ..., usage_kind }, enclosing_method_full_name } ] } ]
    *
    * anchor 레벨의 view_type / usage_type / declaration_scope 를 각 usage에 주입하여
    * AnchorUsageInput으로 생성한다.
    */
  def parseAnchorUsages(path: String): List[AnchorUsageInput] = {
    val json = readJson(path)
    json.arr.toList.flatMap { report =>
      val obj = report.obj

      val anchorMap = obj.get("anchor")
        .flatMap(_.objOpt)
        .map(_.value)
        .getOrElse(collection.mutable.Map.empty[String, ujson.Value])

      val viewType        = str(anchorMap, "view_type")
      val anchorUsageType = str(anchorMap, "usage_type")
      val declarationScope = strOpt(anchorMap, "declaration_scope")

      val usages = obj.get("usages").map(_.arr.toList).getOrElse(Nil)
      usages.flatMap { usageValue =>
        usageValue.objOpt.map { usageObj =>
          val usageMap = usageObj.value

          val usagePointMap = usageMap
            .get("usage_point")
            .flatMap(_.objOpt)
            .map(_.value)
            .getOrElse(collection.mutable.Map.empty[String, ujson.Value])

          AnchorUsageInput(
            anchorId          = str(usageMap, "anchor_id"),
            viewType          = viewType,
            anchorUsageType   = anchorUsageType,
            declarationScope  = declarationScope,
            usagePoint = UsagePoint(
              nodeId    = readId(usagePointMap, "node_id").getOrElse(-1L),
              nodeLabel = str(usagePointMap, "node_label"),
              file      = str(usagePointMap, "file"),
              startLine = int(usagePointMap, "start_line"),
              endLine   = int(usagePointMap, "end_line"),
              code      = str(usagePointMap, "code"),
              usageKind = str(usagePointMap, "usage_kind")
            ),
            enclosingMethodFullName = str(usageMap, "enclosing_method_full_name")
          )
        }
      }
    }
  }

  /** ujson.Obj doesn't accept Seq directly; build via mutable mutation. */
  private def mapObj(entries: Iterable[(String, ujson.Value)]): ujson.Obj = {
    val obj = ujson.Obj()
    entries.foreach { case (k, v) => obj(k) = v }
    obj
  }

  private def writeFile(path: java.nio.file.Path, content: String): Unit = {
    Option(path.getParent).foreach(Files.createDirectories(_))
    Files.write(path, content.getBytes(StandardCharsets.UTF_8))
  }

  // ── Serialization ──────────────────────────────────────────────────────────

  /** 출력을 3개 파일로 분리하여 outputDir 디렉토리에 저장.
    *
    *   slices.json        — 슬라이스 메타데이터 + refs (코드라인 없음)
    *   method-bodies.json — { fullName: MethodInfo } 전역 lookup
    *   type-index.json    — { typeFullName: TypeInfo } 전역 lookup
    *
    * @param outputDir 출력 디렉토리 경로
    * @param slices    집계된 MethodSlice 목록
    * @param globalMethodBodies 전체 슬라이스 methodBodies 합집합
    * @param globalTypeIndex    전체 슬라이스 typeDefinitions 합집합
    */
  def writeOutput(
    outputDir: String,
    slices: List[MethodSlice],
    globalMethodBodies: Map[String, MethodInfo],
    globalTypeIndex: Map[String, TypeInfo]
  ): Unit = {
    val dir = Paths.get(outputDir)

    val slicesJson      = ujson.Arr(slices.map(toSliceJson): _*)
    val methodBodiesObj = mapObj(globalMethodBodies.toSeq.sortBy(_._1).map { case (k, v) => k -> (methodInfoJson(v): ujson.Value) })
    val typeIndexObj    = mapObj(globalTypeIndex.toSeq.sortBy(_._1).map { case (k, v) => k -> (typeInfoJson(v): ujson.Value) })

    writeFile(dir.resolve("slices.json"),        ujson.write(slicesJson,      indent = 2))
    writeFile(dir.resolve("method-bodies.json"),  ujson.write(methodBodiesObj, indent = 2))
    writeFile(dir.resolve("type-index.json"),     ujson.write(typeIndexObj,    indent = 2))
  }

  private def methodInfoJson(m: MethodInfo): ujson.Obj =
    ujson.Obj(
      "method_full_name" -> m.methodFullName,
      "method_id"        -> writeId(m.methodId),
      "method_name"      -> m.methodName,
      "parameter_count"  -> m.parameterCount,
      "unresolved_signature" -> m.unresolvedSignature,
      "file"             -> m.file,
      "start_line"       -> m.startLine,
      "end_line"         -> m.endLine,
      "signature"        -> m.signature,
      "body"             -> m.body
    )

  private def callPathJson(cp: CallPath): ujson.Obj = {
    val base = ujson.Obj(
      "direction"    -> cp.direction,
      "path_methods" -> ujson.Arr(cp.pathMethods.map(ujson.Str(_)): _*)
    )
    cp.rootCallback.foreach(v  => base("root_callback")  = ujson.Str(v))
    cp.callbackKind.foreach(v  => base("callback_kind")  = ujson.Str(v))
    cp.relation.foreach(v      => base("relation")       = ujson.Str(v))
    base
  }

  private def typeInfoJson(t: TypeInfo): ujson.Obj =
    ujson.Obj(
      "type_full_name" -> t.typeFullName,
      "kind"           -> t.kind,
      "file"           -> t.file,
      "start_line"     -> t.startLine,
      "end_line"       -> t.endLine,
      "body"           -> t.body
    )

  private def usagePointJson(up: UsagePoint): ujson.Obj =
    ujson.Obj(
      "node_id"    -> writeId(up.nodeId),
      "node_label" -> up.nodeLabel,
      "file"       -> up.file,
      "start_line" -> up.startLine,
      "end_line"   -> up.endLine,
      "code"       -> up.code,
      "usage_kind" -> up.usageKind
    )

  private def affectingUsageJson(au: AffectingUsage): ujson.Obj = {
    val obj = ujson.Obj(
      "anchor_id"                  -> au.anchorId,
      "view_type"                  -> au.viewType,
      "anchor_usage_type"          -> au.anchorUsageType,
      "usage_point"                -> usagePointJson(au.usagePoint),
      "enclosing_method_full_name" -> au.enclosingMethodFullName
    )
    au.declarationScope match {
      case Some(s) => obj("declaration_scope") = ujson.Str(s)
      case None    => obj("declaration_scope") = ujson.Null
    }
    obj
  }

  /** 슬라이스 직렬화: 실제 코드 제외, refs만 출력.
    *
    *   method_body_refs  — method_bodies 맵의 키 목록 (정렬)
    *   domain_type_refs  — typeDefinitions 맵의 키 목록 (정렬)
    */
  private def toSliceJson(entry: MethodSlice): ujson.Obj = {
    val obj = ujson.Obj(
      "primary_method"    -> entry.primaryMethod,
      "affecting_usages"  -> ujson.Arr(entry.affectingUsages.map(affectingUsageJson): _*),
      "call_paths"        -> ujson.Arr(entry.callPaths.map(callPathJson): _*),
      "method_body_refs"  -> ujson.Arr(entry.methodBodies.keys.toList.sorted.map(ujson.Str(_)): _*),
      "domain_type_refs"  -> ujson.Arr(entry.typeDefinitions.keys.toList.sorted.map(ujson.Str(_)): _*),
      "root_callbacks"    -> ujson.Arr(entry.rootCallbacks.map(ujson.Str(_)): _*),
      "incomplete"        -> entry.incomplete,
      "validation_errors" -> ujson.Arr(entry.validationErrors.map(ujson.Str(_)): _*)
    )
    entry.methodBodies.get(entry.primaryMethod).foreach { primary =>
      obj("primary_method_id") = writeId(primary.methodId)
      obj("primary_method_name") = ujson.Str(primary.methodName)
      obj("primary_method_unresolved_signature") = ujson.Bool(primary.unresolvedSignature)
    }
    obj
  }
}
