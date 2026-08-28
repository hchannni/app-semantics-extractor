/** Context Slicer 데이터 모델 정의.
  *
  * 파이프라인 흐름:
  *   anchor-usages.json
  *     → AnchorUsageInput[]
  *     → groupBy(outerNonLambdaMethod(enclosingMethodFullName))  ← lambda collapsing
  *     → ContextMethodCollector.collect(primaryFullName, entries) → MethodSlice[]
  *     → ContextSlicerJson.writeOutput()
  *         → context-slicer-output/slices.json        (메타데이터 + refs, 코드 없음)
  *         → context-slicer-output/method-bodies.json (전역 method body lookup)
  *         → context-slicer-output/type-index.json    (전역 앱 도메인 타입 lookup)
  *
  * 설계 기준: docs/PROJECT_GUIDE.md 섹션 C-1~C-4
  */
object ContextSlicerModel {

  // ── CPG node 위치/코드 ──────────────────────────────────────────────────────

  /** CPG 상 usage 노드의 위치 및 코드.
    * usageKind: B 모듈이 분류한 UI 상호작용 유형 (GETTER/SETTER/LISTENER/DELEGATE/OTHER).
    */
  case class UsagePoint(
    nodeId: Long,
    nodeLabel: String,
    file: String,
    startLine: Int,
    endLine: Int,
    code: String,
    usageKind: String
  )

  // ── B 모듈 입력 ─────────────────────────────────────────────────────────────

  /** anchor-usages.json에서 파싱된 usage 1건.
    * B 모듈의 메타정보(viewType, anchorUsageType, declarationScope)를 손실 없이 보존한다.
    */
  case class AnchorUsageInput(
    anchorId: String,
    viewType: String,
    anchorUsageType: String,
    declarationScope: Option[String],
    usagePoint: UsagePoint,
    enclosingMethodFullName: String
  )

  // ── 메서드 정보 ─────────────────────────────────────────────────────────────

  /** 메서드 선언부 + 바디.
    *
    * methodId / methodName / parameterCount / unresolvedSignature 는 동일 CPG run
    * 내부에서 methodFullName이 불완전한 Kotlin method도 후속 단계가 안정적으로
    * 재식별할 수 있도록 보존하는 low-level identity metadata다.
    *
    * signature / body 모두 소스 파일 직접 읽기로 추출 (CPG block.code 미사용).
    *   - signature: 소스 첫 줄 (fun/override fun 선언 라인)
    *   - body:      lineNumber~lineNumberEnd 기준으로 읽은 소스 원문.
    *                {}/() depth balancing으로 CPG lineNumberEnd 부정확성 보정.
    *
    * 직렬화 위치: method-bodies.json (전역 lookup, fullName 키 기반 dedupe).
    *              slices.json에는 method_body_refs(키 목록)만 포함.
    */
  case class MethodInfo(
    methodFullName: String,
    methodId: Long,
    methodName: String,
    parameterCount: Int,
    unresolvedSignature: Boolean,
    file: String,
    startLine: Int,
    endLine: Int,
    signature: String,
    body: String
  )

  // ── 경로 구조 ───────────────────────────────────────────────────────────────

  /** backward / forward 호출 경로.
    * direction:   "BACKWARD" (C-2) | "FORWARD" (C-3)
    * pathMethods: 경로상 메서드 fullName 목록 (순서 보장).
    *              BACKWARD: [primary, hop1, ..., root_callback]
    *              FORWARD:  [primary, hop1, ..., callee]  (실제 hop 경로 보존, depth 3까지)
    * rootCallback / callbackKind / relation: BACKWARD 전용 (FORWARD 시 None/empty).
    */
  case class CallPath(
    direction: String,
    pathMethods: List[String],
    rootCallback: Option[String],
    callbackKind: Option[String],
    relation: Option[String]
  )

  // ── 타입 정의 ───────────────────────────────────────────────────────────────

  /** 앱 도메인 타입의 클래스 정의부 전체 (C-4).
    *
    * 수집 대상: 경로상 메서드 body의 파라미터/로컬변수/식별자/멤버 타입,
    *            primary method enclosing 클래스 필드 타입,
    *            enclosing 클래스 FQN 자체 (필드 선언 전체 포함).
    *
    * kind: "CLASS" | "DATA_CLASS" | "SEALED_CLASS" | "ENUM" | "INTERFACE" | "OBJECT"
    *
    * 직렬화 위치: type-index.json (전역 lookup, typeFullName 키 기반 dedupe).
    *              slices.json에는 domain_type_refs(키 목록)만 포함.
    */
  case class TypeInfo(
    typeFullName: String,
    kind: String,
    file: String,
    startLine: Int,
    endLine: Int,
    body: String
  )

  // ── Usage 정보 ──────────────────────────────────────────────────────────────

  /** B 모듈 usage 정보. affecting_usages[] 항목.
    * B 모듈의 메타정보를 손실 없이 전달한다.
    */
  case class AffectingUsage(
    anchorId: String,
    viewType: String,
    anchorUsageType: String,
    declarationScope: Option[String],
    usagePoint: UsagePoint,
    enclosingMethodFullName: String
  )

  // ── 최종 출력 단위 ─────────────────────────────────────────────────────────

  /** 메서드 1건에 대한 최종 결과. slices.json의 최상위 단위.
    *
    * methodBodies / typeDefinitions 는 직렬화 시 refs(키 목록)만 출력하고
    * 실제 코드는 전역 lookup 파일(method-bodies.json, type-index.json)에 저장된다.
    *
    * primaryMethod:    이 슬라이스의 기준 primary method fullName.
    * methodBodies:     fullName → MethodInfo 맵 (경로상 모든 메서드, dedupe). 집계용.
    * callPaths:        BACKWARD + FORWARD 경로 목록.
    * typeDefinitions:  fullName → TypeInfo 맵 (앱 도메인 타입 정의). 집계용.
    * rootCallbacks:    발견된 Android root callback fullName 목록 (shorthand).
    */
  case class MethodSlice(
    primaryMethod: String,
    affectingUsages: List[AffectingUsage],
    callPaths: List[CallPath],
    methodBodies: Map[String, MethodInfo],
    typeDefinitions: Map[String, TypeInfo],
    rootCallbacks: List[String],
    incomplete: Boolean,
    validationErrors: List[String]
  )
}
