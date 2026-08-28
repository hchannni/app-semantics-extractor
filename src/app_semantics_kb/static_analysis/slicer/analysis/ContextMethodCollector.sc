import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.collection.mutable
import scala.util.Try
import java.nio.file.Paths

/** Context Method Collector.
  *
  * 담당:
  *   C-1. Method Body Extraction  — 모든 경로 메서드의 signature + body 수집 (공통)
  *                                   소스 파일 직접 읽기 + {}/() depth balancing으로 완전한 본문 추출
  *   C-2. Backward (위임)         — ContextCallbackResolver.resolveCallbackPaths()
  *   C-3. Forward Domain Tracking — primary effective scope(본문 + 람다 바디) 내 domain callee depth-3 수집
  *   C-4. Type Resolution (위임)  — TypeResolution.collectFieldTypeNames / enclosingTypeName / resolveTypeDefs
  *                                   (class_fields 폐기됨; enclosing class를 type-index에 직접 포함)
  *
  * 출력:
  *   MethodSlice — methodBodies/typeDefinitions 맵은 집계용으로 유지.
  *   실제 직렬화는 ContextSlicerJson.writeOutput 에서 3파일로 분리:
  *     slices.json / method-bodies.json / type-index.json
  *
  * 설계 기준: docs/PROJECT_GUIDE.md 섹션 C-1~C-4
  */
object ContextMethodCollector {
  import ContextSlicerModel.*

  private val frameworkPrefixes = List(
    "android.", "androidx.", "java.", "kotlin.", "com.google.android.material."
  )

  // ── 헬퍼 ──────────────────────────────────────────────────────────────────

  private def isFramework(fullName: String): Boolean =
    frameworkPrefixes.exists(p => fullName.startsWith(p))

  private def hasUnresolvedSignature(fullName: String): Boolean =
    fullName.contains(":<unresolved")

  private def hasUnresolvedSignature(method: Method): Boolean =
    hasUnresolvedSignature(Option(method.fullName).getOrElse("")) ||
      Option(method.signature).exists(_.contains("<unresolvedSignature>"))

  private def isSyntheticOrUnknown(fullName: String): Boolean =
    fullName.startsWith("<operator>.") ||
      fullName.startsWith("<unresolved") ||
      fullName.startsWith("<unknown>")

  private def isDomainMethod(fullName: String): Boolean =
    fullName.nonEmpty && !isFramework(fullName) && !isSyntheticOrUnknown(fullName)

  private def isFileBacked(method: Method): Boolean = {
    val file = method.file.name.headOption.getOrElse("")
    file.nonEmpty && file != "<unknown>"
  }

  private def methodNameFromFullName(fullName: String): String = {
    val beforeSignature = fullName.takeWhile(_ != ':')
    val lastDot = beforeSignature.lastIndexOf('.')
    if (lastDot >= 0) beforeSignature.substring(lastDot + 1) else beforeSignature
  }

  private def parameterCount(method: Method): Int =
    method.parameter.l.count(p => Option(p.name).forall(_ != "this"))

  // ── C-1: Method Body Extraction ───────────────────────────────────────────

  /** C-1a. 원본 소스 파일에서 메서드 전체 텍스트를 추출.
    *
    * method.lineNumber ~ method.lineNumberEnd 범위를 파일에서 직접 읽어
    * IDE에서 보는 것과 동일한 텍스트(시그니처 + 바디 + 닫는 괄호)를 반환한다.
    * sourcePath가 비어있거나 파일을 읽을 수 없으면 None 반환.
    */
  // NOTE: resolveSourceFile은 TypeResolution.resolveSourceFile로 이동됨.
  // NOTE: extendToBalanced is intentionally removed; balanced extension is inlined in methodBodyFromFile

  private def methodBodyFromFile(method: Method, sourcePath: String): Option[String] = {
    if (sourcePath.isEmpty) return None
    val relFile   = method.file.name.headOption.getOrElse("")
    val startLine = method.lineNumber.map(_.toInt).getOrElse(-1)
    if (startLine <= 0) return None

    val fileOpt = {
      val p = Paths.get(relFile)
      if (p.isAbsolute) Some(p.toFile).filter(_.exists())
      else TypeResolution.resolveSourceFile(relFile, sourcePath)
    }

    val methodEnd = method.lineNumberEnd.map(_.toInt).getOrElse(-1)
    val blockEnd  = Option(method.block).map(b => TypeResolution.lineRange(b)._2).getOrElse(-1)
    val cpgEnd    = List(methodEnd, blockEnd).filter(_ > 0).maxOption.getOrElse(startLine)

    fileOpt.flatMap { file =>
      Try {
        val src      = scala.io.Source.fromFile(file)
        val allLines = try src.getLines().toIndexedSeq finally src.close()
        val initial  = allLines.slice(startLine - 1, cpgEnd)
        // CPG lineNumberEnd가 짧을 때를 위한 보정:
        // initial 범위의 {} + () 불균형을 측정하고, 불균형이 있으면
        // cpgEnd 이후부터 이어 읽어 모두 닫힐 때까지 추가한다.
        // ({}가 닫히지 않은 single-expression fn, ()가 닫히지 않은 data class 생성자 등 처리)
        val braceImbal = initial.foldLeft(0)((d, l) => d + l.count(_ == '{') - l.count(_ == '}'))
        val parenImbal = initial.foldLeft(0)((d, l) => d + l.count(_ == '(') - l.count(_ == ')'))
        val needsExt   = braceImbal > 0 || parenImbal > 0
        if (needsExt) {
          allLines.drop(cpgEnd)
            .foldLeft((initial.toVector, braceImbal, parenImbal)) { (st, line) =>
              if (st._2 == 0 && st._3 == 0) st  // 이미 balanced
              else {
                val nb = st._2 + line.count(_ == '{') - line.count(_ == '}')
                val np = st._3 + line.count(_ == '(') - line.count(_ == ')')
                (st._1 :+ line, nb, np)
              }
            }
            ._1.mkString("\n")
        } else {
          initial.mkString("\n")
        }
      }.toOption.filter(_.nonEmpty)
    }
  }

  /** C-1. 메서드 선언부(signature) + 본문(body)을 추출하여 MethodInfo로 반환.
    *
    * 1순위: sourcePath가 있으면 파일에서 전체 텍스트 추출
    *   body      = 전체 함수 텍스트 (시그니처 + { ... })
    *   signature = body의 첫 번째 non-empty 줄
    * 2순위 (fallback): method.block.code
    *   body      = 바디 내용만
    *   signature = method.code (이름만)
    */
  private def methodInfoOf(method: Method, sourcePath: String = ""): MethodInfo = {
    val (startLine, endLine) = Option(method.block)
      .map(TypeResolution.lineRange)
      .getOrElse(TypeResolution.lineRange(method))

    val fileText = methodBodyFromFile(method, sourcePath)

    val signature = fileText
      .flatMap(_.linesIterator.find(_.trim.nonEmpty))
      .getOrElse(Option(method.code).filter(_.nonEmpty).getOrElse(""))

    val body = fileText.getOrElse {
      Option(method.block)
        .flatMap(b => Option(b.code))
        .filter(_.nonEmpty)
        .orElse(Option(method.code).filter(_.nonEmpty))
        .getOrElse("")
    }

    MethodInfo(
      methodFullName = Option(method.fullName).getOrElse(""),
      methodId       = method.id,
      methodName     = Option(method.name).filter(_.nonEmpty).getOrElse(methodNameFromFullName(Option(method.fullName).getOrElse(""))),
      parameterCount = parameterCount(method),
      unresolvedSignature = hasUnresolvedSignature(method),
      file           = method.file.name.headOption.getOrElse(""),
      startLine      = startLine,
      endLine        = endLine,
      signature      = signature,
      body           = body
    )
  }

  private def resolveMethodInfo(fullName: String, sourcePath: String)(implicit cpg: Cpg): Option[MethodInfo] = {
    if (!isDomainMethod(fullName)) None
    else cpg.method.fullNameExact(fullName).headOption.filter(isFileBacked).map(methodInfoOf(_, sourcePath))
  }

  // ── C-3: Forward Domain Call Tracking ────────────────────────────────────

  /** C-3. Primary method의 Effective Scope 내 domain callee를 재귀적으로 수집.
    *
    * Effective Scope:
    *   1. Primary method 본문의 Call 노드
    *   2. Primary method 내 MethodRef → lambda/anonymous Method 노드의 Call 노드
    *
    * depth: 최대 3 level 재귀 탐색.
    * 반환: Map[callee fullName → 완전 경로 [primary, hop1, ..., callee]]
    *   - 동일 callee에 먼저 도달한 경로를 보존 (중복 방지).
    */
  private def collectEffectiveScopeCalleePaths(
    method: Method,
    maxDepth: Int
  )(implicit cpg: Cpg): Map[String, List[String]] = {
    val primaryFn = Option(method.fullName).getOrElse("")
    val pathMap   = mutable.Map.empty[String, List[String]]

    def collectDirect(m: Method): Set[String] = {
      val directCalls = m.ast
        .collect { case call: Call => call }
        .l
        .flatMap(c => Option(c.methodFullName))
        .filter(isDomainMethod)
        .filterNot(_ == Option(m.fullName).getOrElse(""))
        .toSet

      val lambdaCalls = m.ast
        .collect { case ref: MethodRef => ref }
        .l
        .flatMap(ref => Option(ref.methodFullName))
        .filter(TypeResolution.isLambdaOrAnonymous)
        .flatMap(lambdaFn => cpg.method.fullNameExact(lambdaFn).l)
        .flatMap { lambdaMethod =>
          lambdaMethod.ast
            .collect { case call: Call => call }
            .l
            .flatMap(c => Option(c.methodFullName))
            .filter(isDomainMethod)
            .filterNot(_ == Option(m.fullName).getOrElse(""))
        }
        .toSet

      directCalls ++ lambdaCalls
    }

    def recurse(names: Set[String], depth: Int, parentPath: List[String]): Unit = {
      if (depth > maxDepth) return
      val newNames = names.filterNot(n => pathMap.contains(n) || n == primaryFn)
      if (newNames.isEmpty) return
      newNames.foreach { fn =>
        val path = parentPath :+ fn
        pathMap(fn) = path
        cpg.method.fullNameExact(fn).headOption.filter(isFileBacked).foreach { m =>
          recurse(collectDirect(m), depth + 1, path)
        }
      }
    }

    recurse(collectDirect(method), depth = 1, parentPath = List(primaryFn))
    pathMap.toMap.filter { case (fn, _) => isDomainMethod(fn) }
  }

  /** C-3. Forward callee 경로 맵을 CallPath(direction=FORWARD) 엔트리 목록으로 변환.
    * pathMethods에 [primary, hop1, ..., callee] 완전 경로를 그대로 저장한다.
    */
  private def forwardCallPaths(
    primaryFullName: String,
    calleePaths: Map[String, List[String]]
  ): List[CallPath] =
    calleePaths
      .filter { case (callee, _) => callee != primaryFullName }
      .map { case (_, path) =>
        CallPath(
          direction    = "FORWARD",
          pathMethods  = path,
          rootCallback = None,
          callbackKind = None,
          relation     = None
        )
      }
      .toList
      .sortBy(_.pathMethods.lastOption.getOrElse(""))

  // ── collect() ─────────────────────────────────────────────────────────────

  /** C-1~C-4 수집 및 MethodSlice 생성.
    *
    * @param primaryFullName  outer non-lambda method fullName (groupBy 키)
    * @param entries          같은 primaryFullName으로 collapse된 AnchorUsageInput 목록
    *                         (lambda entries 포함, 원본 enclosingMethodFullName 보존)
    */
  def collect(
    primaryFullName: String,
    entries: List[AnchorUsageInput],
    sourcePath: String = ""
  )(implicit cpg: Cpg): MethodSlice = {
    val validationErrors = mutable.ListBuffer.empty[String]

    entries.foreach { e =>
      if (e.usagePoint.nodeId <= 0)
        validationErrors += s"invalid usage_point.node_id: ${e.usagePoint.nodeId} (anchor=${e.anchorId})"
    }

    // 1. Primary method 결정 (outer non-lambda)
    val primaryMethodOpt = cpg.method.fullNameExact(primaryFullName).headOption
    if (primaryMethodOpt.isEmpty) {
      validationErrors += s"primary_method not found in CPG: '$primaryFullName'"
    }

    val primaryInfo = primaryMethodOpt.map(methodInfoOf(_, sourcePath)).getOrElse(
      MethodInfo(
        methodFullName       = primaryFullName,
        methodId             = -1L,
        methodName           = methodNameFromFullName(primaryFullName),
        parameterCount       = -1,
        unresolvedSignature  = hasUnresolvedSignature(primaryFullName),
        file                 = "",
        startLine            = -1,
        endLine              = -1,
        signature            = "",
        body                 = ""
      )
    )
    if (primaryInfo.body.isEmpty || primaryInfo.body == "<empty>") {
      validationErrors += "primary_method.body is empty"
    }

    // 2. C-2: Backward 경로 수집 (1회)
    val backwardPaths = ContextCallbackResolver.resolveCallbackPaths(primaryFullName)

    // 3. C-3: Forward 도메인 callee 수집 (outer method 기준, lambda 바디 포함)
    //    calleePaths: callee fullName → [primary, hop1, ..., callee] 완전 경로
    val forwardCalleePaths: Map[String, List[String]] = primaryMethodOpt
      .map(m => collectEffectiveScopeCalleePaths(m, maxDepth = 5))
      .getOrElse(Map.empty)
    val forwardPaths = forwardCallPaths(primaryFullName, forwardCalleePaths)

    // 3-b. C-3 보강: root_callback 발 forward 추적 (helper recovery).
    //   anchor가 직접 닿지 않는 helper 메서드(R.id를 다루지 않아 anchor-usages에 없고
    //   어떤 슬라이스의 primary_method도 아닌 메서드)는 root_callback(예: onCreateView)에서
    //   호출되는데, 현재 forward 시드는 primary 한 종류라 그 forward closure가 누락된다.
    //   root_callback도 forward 시드로 추가해 helper 본문/타입을 회복한다.
    //
    //   차집합 정책:
    //     - primary 발 forward closure에 이미 있는 callee는 제외 (중복 제거).
    //     - primary 자신은 제외.
    //     - 여러 root_callback 사이에는 first-arrival을 보존.
    //
    //   pathMethods[0]은 root_callback fullName이 되어 "왜 그 helper가 들어왔는지"의
    //   explainability가 유지된다.
    val rootCallbackFullNames = backwardPaths.flatMap(_.rootCallback).filter(_.nonEmpty).distinct
    val primaryForwardKnown   = forwardCalleePaths.keySet + primaryFullName
    val rootCallbackForwardPaths: Map[String, List[String]] =
      rootCallbackFullNames.foldLeft(Map.empty[String, List[String]]) { (acc, rcFn) =>
        cpg.method.fullNameExact(rcFn).headOption.filter(isFileBacked) match {
          case Some(rcMethod) =>
            val rcPaths = collectEffectiveScopeCalleePaths(rcMethod, maxDepth = 5)
            val supplemental = rcPaths.filterNot { case (callee, _) =>
              primaryForwardKnown.contains(callee) || acc.contains(callee) || callee == rcFn
            }
            acc ++ supplemental
          case None => acc
        }
      }
    val rootCallbackForwardCallPaths: List[CallPath] =
      rootCallbackForwardPaths
        .map { case (_, path) =>
          CallPath(
            direction    = "FORWARD",
            pathMethods  = path,
            rootCallback = None,
            callbackKind = None,
            relation     = None
          )
        }
        .toList
        .sortBy(_.pathMethods.lastOption.getOrElse(""))

    val forwardCalleeNames: Set[String] =
      forwardCalleePaths.keySet ++ rootCallbackForwardPaths.keySet

    val callPaths = backwardPaths ++ forwardPaths ++ rootCallbackForwardCallPaths

    // 4. C-1: method_bodies — backward + forward 경로 메서드 바디 수집 (1회)
    val backwardMethodNames = backwardPaths.flatMap(_.pathMethods).filter(isDomainMethod).toSet
    val allMethodNames      = (backwardMethodNames ++ forwardCalleeNames + primaryFullName).filter(_.nonEmpty)

    val methodBodies: Map[String, MethodInfo] = {
      val builder = Map.newBuilder[String, MethodInfo]
      if (primaryInfo.methodFullName.nonEmpty) builder += primaryInfo.methodFullName -> primaryInfo
      (allMethodNames - primaryFullName).foreach { fn =>
        resolveMethodInfo(fn, sourcePath).foreach(info => builder += fn -> info)
      }
      // outer method body가 lambda 코드를 이미 인라인으로 포함하므로 lambda/anonymous key 제거.
      // <init> 생성자는 type-index.json의 TypeInfo body와 완전 중복이므로 제거.
      builder.result().filter { case (fn, _) =>
        !TypeResolution.isLambdaOrAnonymous(fn) && !fn.contains(".<init>:")
      }
    }

    // 5. root_callbacks shorthand
    val rootCallbacks = backwardPaths.flatMap(_.rootCallback).filter(_.nonEmpty).distinct.sorted

    // 6. C-4: type_definitions
    //    - 경로상 모든 메서드의 파라미터/멤버/로컬변수/식별자 타입 (extractReferencedTypeNames)
    //    - primary method enclosing 클래스 멤버 필드의 타입 이름 (collectFieldTypeNames)
    //    - enclosing 클래스 FQN (enclosingTypeName) → class body 전체가 type-index에 포함됨
    val allMethodNodes   = allMethodNames.flatMap(fn => cpg.method.fullNameExact(fn).l.headOption)
    val fieldTypeNames   = primaryMethodOpt.map(TypeResolution.collectFieldTypeNames).getOrElse(Set.empty)
    val enclosingType    = primaryMethodOpt.flatMap(TypeResolution.enclosingTypeName).toSet
    val typeDefinitions  = TypeResolution.resolveTypeDefs(
      TypeResolution.extractReferencedTypeNames(allMethodNodes, fieldTypeNames) ++ enclosingType,
      sourcePath
    )

    // affecting_usages: entries 전체 (원본 enclosingMethodFullName 보존, lambda 포함)
    val affectingUsages = entries
      .map(e => AffectingUsage(
        anchorId                = e.anchorId,
        viewType                = e.viewType,
        anchorUsageType         = e.anchorUsageType,
        declarationScope        = e.declarationScope,
        usagePoint              = e.usagePoint,
        enclosingMethodFullName = e.enclosingMethodFullName
      ))
      .groupBy(au => (au.anchorId, au.usagePoint.nodeId))
      .values.map(_.head).toList
      .sortBy(au => (au.anchorId, au.usagePoint.file, au.usagePoint.startLine, au.usagePoint.nodeId))

    MethodSlice(
      primaryMethod    = primaryFullName,
      affectingUsages  = affectingUsages,
      callPaths        = callPaths,
      methodBodies     = methodBodies,
      typeDefinitions  = typeDefinitions,
      rootCallbacks    = rootCallbacks,
      incomplete       = validationErrors.nonEmpty,
      validationErrors = validationErrors.toList.distinct
    )
  }
}
