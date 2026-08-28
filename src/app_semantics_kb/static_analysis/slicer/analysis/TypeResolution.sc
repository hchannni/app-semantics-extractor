import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import java.nio.file.Paths
import scala.util.Try

/** Type Resolution & Lambda Collapsing.
  *
  * 담당:
  *   C-4a. Enclosing Type Resolution — primary method enclosing 클래스 FQN + 멤버 필드 타입 이름 수집
  *                                      (class_fields 폐기; enclosing class 자체를 type-index에 포함)
  *   C-4b. Type Definition Collection — 참조된 앱 도메인 타입 정의(클래스 body 전문) 수집
  *                                      수집 소스: 파라미터 / 로컬변수 / 식별자 / 멤버 타입
  *   Lambda Collapsing               — lambda/anonymous/object$ fullName → outer non-synthetic method
  *                                      (method body 집계 및 primary method groupBy 기준에 사용)
  *
  * 설계 기준: docs/PROJECT_GUIDE.md 섹션 C-4
  */
object TypeResolution {
  import ContextSlicerModel.*

  private val frameworkPrefixes = List(
    "android.", "androidx.", "java.", "kotlin.", "com.google.android.material."
  )

  // ── Synthetic method detection ─────────────────────────────────────────────

  /** Kotlin lambda / anonymous class / object expression 판별.
    * fullName 패턴: .<lambda>N | .<anonymous>N | .object$N
    */
  def isLambdaOrAnonymous(fullName: String): Boolean =
    fullName.contains("<lambda>") ||
      fullName.contains("<anonymous>") ||
      fullName.contains(".object$")

  // ── Lambda Collapsing ──────────────────────────────────────────────────────

  /** lambda/anonymous/object$ fullName에서 외부 non-synthetic 메서드 fullName을 반환.
    *
    * 전략: 첫 synthetic 세그먼트(.<lambda> | .<anonymous> | .object$) 직전 prefix를 추출,
    *       CPG에서 `prefix:` 로 시작하는 non-synthetic 메서드를 탐색.
    *       CPG에 없으면 원본 fullName 반환 (collect에서 validation error 기록).
    *
    * 예시:
    *   "Foo.bar.<lambda>3:void()"              → "Foo.bar:void(...)"
    *   "Foo.bar.<lambda>3.<lambda>7:void()"    → "Foo.bar:void(...)"
    *   "Foo.bar.<lambda>3.object$5.onScroll:." → "Foo.bar:void(...)"
    *   "Foo.bar.object$5.<init>:void()"        → "Foo.bar:void(...)"
    *   non-synthetic                           → 그대로 반환
    */
  def outerNonLambdaMethod(fullName: String)(implicit cpg: Cpg): String = {
    if (!isLambdaOrAnonymous(fullName)) return fullName

    val colonIdx = fullName.lastIndexOf(':')
    val namePart = if (colonIdx > 0) fullName.substring(0, colonIdx) else fullName

    val lambdaIdx = namePart.indexOf(".<lambda>")
    val anonIdx   = namePart.indexOf(".<anonymous>")
    val objectIdx = namePart.indexOf(".object$")

    val cutIdx = Seq(lambdaIdx, anonIdx, objectIdx).filter(_ >= 0).minOption.getOrElse(-1)
    if (cutIdx < 0) return fullName

    val prefix = namePart.substring(0, cutIdx)

    cpg.method
      .filter(m =>
        m.fullName.startsWith(prefix + ":") &&
          !isLambdaOrAnonymous(m.fullName)
      )
      .headOption
      .map(_.fullName)
      .getOrElse(fullName)
  }

  // ── Shared file utilities ──────────────────────────────────────────────────

  /** CPG 파일 경로(relFile)가 실제 소스 트리와 prefix가 다를 수 있으므로
    * 선두 path component를 하나씩 제거해가며 sourcePath 하위에서 파일을 찾는다.
    *
    * 예) CPG: "app/src/main/java/com/foo/Bar.kt"
    *     실제: sourcePath + "/main/java/com/foo/Bar.kt"  (app/src/ 제거됨)
    *
    * ContextMethodCollector.resolveSourceFile과 동일한 로직 (공유 위치).
    */
  def resolveSourceFile(relFile: String, sourcePath: String): Option[java.io.File] = {
    if (relFile.isEmpty || relFile == "<unknown>") return None
    val base  = Paths.get(sourcePath)
    val parts = relFile.split("/").filter(_.nonEmpty)
    (0 until parts.length).iterator
      .map(i => base.resolve(parts.drop(i).mkString("/")).normalize().toFile)
      .find(_.exists())
  }

  /** TypeDecl의 소스 파일로부터 클래스 정의 전문을 추출.
    *
    * td.code는 클래스 이름만 저장하므로 파일 직접 읽기로 대체.
    * methodBodyFromFile과 동일한 {}/() depth balancing 적용.
    */
  private def typeBodyFromFile(td: TypeDecl, sourcePath: String): Option[String] = {
    if (sourcePath.isEmpty) return None
    val relFile = td.file.name.headOption.getOrElse("")
    // TypeDecl에는 lineNumberEnd가 없으므로 lineRange(td)로 AST 전체 스캔
    val (startLine, astEnd) = lineRange(td)
    if (startLine <= 0) return None

    val fileOpt = {
      val p = Paths.get(relFile)
      if (p.isAbsolute) Some(p.toFile).filter(_.exists())
      else resolveSourceFile(relFile, sourcePath)
    }

    val cpgEnd = if (astEnd > startLine) astEnd else startLine

    fileOpt.flatMap { file =>
      Try {
        val src      = scala.io.Source.fromFile(file)
        val allLines = try src.getLines().toIndexedSeq finally src.close()
        val initial  = allLines.slice(startLine - 1, cpgEnd)
        val braceImbal = initial.foldLeft(0)((d, l) => d + l.count(_ == '{') - l.count(_ == '}'))
        val parenImbal = initial.foldLeft(0)((d, l) => d + l.count(_ == '(') - l.count(_ == ')'))
        if (braceImbal > 0 || parenImbal > 0) {
          allLines.drop(cpgEnd)
            .foldLeft((initial.toVector, braceImbal, parenImbal)) { (st, line) =>
              if (st._2 == 0 && st._3 == 0) st
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

  // ── Shared helpers ─────────────────────────────────────────────────────────

  def lineRange(node: AstNode): (Int, Int) = {
    val lines =
      (node.lineNumber.toList ++ node.ast.collect { case n: AstNode => n }.l.flatMap(_.lineNumber))
        .filter(_ >= 0)
    if (lines.isEmpty) (-1, -1) else (lines.min, lines.max)
  }

  def isDomainType(typeFullName: String): Boolean =
    typeFullName.nonEmpty &&
      !frameworkPrefixes.exists(p => typeFullName.startsWith(p)) &&
      !typeFullName.startsWith("<") &&
      !typeFullName.contains(":<unresolved") &&
      !typeFullName.startsWith("[]") &&
      !Set("boolean", "byte", "char", "short", "int", "long", "float", "double", "void",
           "Boolean", "Byte", "Char", "Short", "Int", "Long", "Float", "Double",
           "String", "Unit", "Any", "Nothing").contains(typeFullName) &&
      !typeFullName.startsWith("java.lang.") &&
      // Identifier 스캔에서 유입될 수 있는 lambda/anonymous object 타입 제외
      !isLambdaOrAnonymous(typeFullName)

  // ── C-4a: Enclosing Type ───────────────────────────────────────────────────

  /** C-4a. primary method의 enclosing 클래스 FQN 반환.
    *
    * class_fields 대신 enclosing 클래스 자체를 type-index에 포함시키는 방식.
    * TypeDecl의 body에 모든 필드 선언이 포함되므로 별도 FieldInfo 불필요.
    */
  def enclosingTypeName(method: Method): Option[String] =
    method.definingTypeDecl.l.headOption
      .map(_.fullName)
      .filter(n => n.nonEmpty && isDomainType(n))

  /** C-4a-sub. enclosing 클래스 멤버 필드 타입 이름 수집 (type_definitions 해상도 입력용).
    *
    * 필드 이름/코드는 불필요; 타입 이름만 추출해 resolveTypeDefs에 전달한다.
    */
  def collectFieldTypeNames(method: Method): Set[String] =
    method.definingTypeDecl.l.flatMap { td =>
      td.member.l.flatMap { member =>
        Option(member.typeFullName)
          .filter(n => n.nonEmpty && n != "<empty>" && n != "<unknown>" && !n.startsWith("<unresolved"))
      }
    }.toSet

  // ── C-4b: Type Definitions ─────────────────────────────────────────────────

  private def resolveTypeKind(typeDecl: TypeDecl): String = {
    val firstLine = Option(typeDecl.code)
      .getOrElse("")
      .linesIterator
      .find(_.trim.nonEmpty)
      .getOrElse("")
      .trim
      .toLowerCase
    if (firstLine.contains("data class"))                              "DATA_CLASS"
    else if (firstLine.contains("sealed class"))                      "SEALED_CLASS"
    else if (firstLine.contains("enum class") || firstLine.contains("enum ")) "ENUM"
    else if (firstLine.contains("interface"))                         "INTERFACE"
    else if (firstLine.contains("object"))                            "OBJECT"
    else                                                              "CLASS"
  }

  /** 앱 도메인 타입 fullName 집합으로부터 TypeInfo 맵을 생성.
    *
    * body 추출 우선순위:
    *   1순위: sourcePath가 있으면 파일에서 클래스 정의 전문 추출 (typeBodyFromFile)
    *   2순위 (fallback): td.code — 클래스 이름만 저장되므로 실질적으로 미사용
    *
    * sourcePath는 CPG 상대경로를 실제 소스 트리에 매핑할 루트 경로.
    */
  def resolveTypeDefs(typeFullNames: Set[String], sourcePath: String = "")(implicit cpg: Cpg): Map[String, TypeInfo] =
    typeFullNames.filter(isDomainType).flatMap { tfn =>
      cpg.typeDecl.fullNameExact(tfn).l.headOption.flatMap { td =>
        val file = td.file.name.headOption.getOrElse("")
        if (file.isEmpty || file == "<unknown>") None
        else {
          val (startLine, endLine) = lineRange(td)
          val body = typeBodyFromFile(td, sourcePath)
            .filter(_.nonEmpty)
            .getOrElse(Option(td.code).filter(_.nonEmpty).getOrElse(""))
          // body가 비어있거나 클래스 이름 단독(= td.code fallback 결과)이면 제외
          if (body.isEmpty || body == td.name) None
          else Some(tfn -> TypeInfo(
            typeFullName = tfn,
            kind         = resolveTypeKind(td),
            file         = file,
            startLine    = startLine,
            endLine      = endLine,
            body         = body
          ))
        }
      }
    }.toMap

  def extractReferencedTypeNames(
    methods: Iterable[Method],
    extraFieldTypes: Iterable[String]
  )(implicit cpg: Cpg): Set[String] = {
    // Lambda 분리 보정: Joern Kotlin frontend는 람다/익명/object$ 식을 *별도*
    // Method 노드로 저장하므로 parent.ast로는 람다 본문 안의 typeFullName 참조를
    // 볼 수 없다. 회귀 사례: `showTimePicker.<lambda>47.<lambda>48` 안의
    // `picked.get()` 반환 타입 `PickedTime`은 parent showTimePicker.ast에 미존재.
    // 따라서 type extraction 대상에 nested synthetic child Method 본문을 합류시킨다.
    val expanded: Set[Method] =
      methods.toSet ++ methods.flatMap(nestedSyntheticChildren).toSet

    val fromMethods = expanded.flatMap { m =>
      val paramTypes   = m.parameter.l.flatMap(p => Option(p.typeFullName))
      val memberTypes  = m.definingTypeDecl.l.flatMap(_.member.l.flatMap(mem => Option(mem.typeFullName)))
      // 메서드 body 내 로컬 변수 선언(val/var) 타입 — 설계 의도의 핵심:
      // "body 내에서 사용되는 식별자의 타입이 앱 도메인 타입이면 그 정의를 포함"
      val localTypes   = m.ast.collect { case l: Local => l }.l.flatMap(l => Option(l.typeFullName))
      // 식별자 참조 타입 — 로컬로 선언되지 않아도 참조만 된 경우 보완
      val identTypes   = m.ast.collect { case id: Identifier => id }.l.flatMap(id => Option(id.typeFullName))
      // 호출 노드의 typeFullName — static receiver / return type 회복.
      //   예) `AlarmApplication.startOnce(...)`, `Intents.EXTRA_ID`,
      //       `picked.get()`의 결과 타입 `PickedTime`(nested lambda 안) 등.
      val callTypes    = m.ast.collect { case c: Call => c }.l.flatMap(c => Option(c.typeFullName))
      // TypeRef — `is Foo`, `as Foo`, `Foo::class` 등 instanceOf/cast 대상 타입.
      val typeRefTypes = m.ast.collect { case t: TypeRef => t }.l.flatMap(t => Option(t.typeFullName))
      paramTypes ++ memberTypes ++ localTypes ++ identTypes ++ callTypes ++ typeRefTypes
    }
    val raw = (fromMethods ++ extraFieldTypes).toSet
    // Generic 인자 분해: `Outer<Inner1, Inner2>` → Outer + Inner1 + Inner2.
    //   Joern Kotlin frontend는 generic을 typeFullName 문자열에 보존하지만 별도
    //   노드로 분리하지 않아 inner 타입(예: `Optional<PickedTime>`의 `PickedTime`)이
    //   누락된다. 단순 split으로 후보로 풀고 isDomainType이 framework/primitive를 거름.
    raw.flatMap(splitGenericArgs)
  }

  /** parent method 아래의 모든 깊이의 nested synthetic Method 노드(`<lambda>N`,
    * `<anonymous>N`, `object$N`)를 한 번에 수집.
    *
    * 매칭 전략:
    *   prefix = method.fullName.takeUntilFirstColon       // 예: "Foo.bar"
    *   regex  = "^" + quote(prefix) + "\\..*"             // greedy `.*`로 모든 깊이
    *   filter = isLambdaOrAnonymous(child.fullName)       // sibling 비람다 방어
    *
    * `.*`가 점을 포함해 매치되므로 `Foo.bar.<lambda>0.<lambda>1` 같은 다중 nested도
    * 1회 정규식으로 모두 잡힘 (재귀 호출 불필요).
    *
    * 본 헬퍼는 *type extraction 대상에 합류*시킬 nested 메서드만 반환한다.
    * forward callee tracking은 ContextMethodCollector가 별도로 다루며 영향 없음.
    */
  private def nestedSyntheticChildren(method: Method)(implicit cpg: Cpg): Iterable[Method] = {
    val fn       = method.fullName
    val colonIdx = fn.indexOf(':')
    val prefix   = if (colonIdx > 0) fn.substring(0, colonIdx) else fn
    if (prefix.isEmpty) Iterable.empty
    else {
      val regex = "^" + java.util.regex.Pattern.quote(prefix) + "\\..*"
      cpg.method.fullName(regex).filter(m => isLambdaOrAnonymous(m.fullName)).l
    }
  }

  /** typeFullName 문자열을 outer + generic 인자들로 분해.
    *
    * 입력 예:
    *   "java.util.Optional<com.better.alarm.ui.timepicker.PickedTime>"
    *     → Set("java.util.Optional", "com.better.alarm.ui.timepicker.PickedTime")
    *   "kotlin.collections.Map<java.lang.String, com.foo.Bar>"
    *     → Set("kotlin.collections.Map", "java.lang.String", "com.foo.Bar")
    *   "Foo" (no generic) → Set("Foo")
    *
    * 보수적 정책:
    *   - `<`로 시작하는 typeFullName(`<unresolved...>`, `<lambda>...`)은 split하지 않고 그대로 둔다.
    *   - split 후 빈 토큰은 제거.
    *   - 후보들은 호출자 측 isDomainType / resolveTypeDefs에서 다시 필터링됨.
    */
  private def splitGenericArgs(typeFullName: String): Set[String] = {
    if (typeFullName.isEmpty) return Set.empty
    if (typeFullName.startsWith("<")) return Set(typeFullName)
    val ltIdx = typeFullName.indexOf('<')
    if (ltIdx < 0) return Set(typeFullName)
    val outer = typeFullName.substring(0, ltIdx).trim
    val inner = typeFullName.substring(ltIdx + 1)
      .reverse.dropWhile(_ != '>').drop(1).reverse  // strip trailing '>' (and any suffix)
    val parts = inner.split("[,<>]").iterator.map(_.trim).filter(_.nonEmpty).toSet
    Set(outer).filter(_.nonEmpty) ++ parts
  }
}
