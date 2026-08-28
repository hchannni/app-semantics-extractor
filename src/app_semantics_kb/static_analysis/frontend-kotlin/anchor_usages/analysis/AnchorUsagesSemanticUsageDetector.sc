import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

/**
 * AnchorUsagesTargetResolver가 결정한 타깃 노드 하나를 받아,
 * 해당 노드가 어떤 종류의 의미론적 사용인지 판별하고 SemanticUsage 리스트를 반환하는 dispatcher.
 *
 * ── 동작 구조 ──────────────────────────────────────────────────────────────────
 *
 * 1. SCOPE early-exit
 *    usageType == "SCOPE"이면 스코프 함수 블록 전체를 단일 SemanticUsage로 반환하고 종료.
 *    (apply/run/with 등의 블록 전체 = 1 usage 라는 설계 원칙)
 *
 * 2. DIRECT_USAGE early-exit
 *    V2 occurrence가 이미 의미론적 operation call을 가리키면 그 call 자체를 분류한다.
 *
 * 3. 5개 케이스 병렬 탐지 (SCOPE/DIRECT_USAGE 외 모든 usageType)
 *    각 케이스 탐지기를 모두 실행해 결과를 합산한다:
 *      - ChainingCase       : usageType == CHAINING 전용. handle/acquisition Call 자체를 먼저 분류.
 *      - PassThroughCase    : 앵커가 다른 메서드의 인자로 전달(Delegate)되거나 return되는 패턴.
 *                             1단계 inter-procedural 추적 포함.
 *      - ClosureCaptureCase : 앵커가 람다/클로저 안에서 사용되는 패턴.
 *                             Path A(MethodRef), Path B(ClosureBinding) 두 경로.
 *      - FieldInstanceCase  : 앵커의 프로퍼티(필드)에 접근하는 패턴. Getter/Setter 분류.
 *      - ReceiverCase       : 앵커가 receiver position에서 호출되는 일반 메서드 패턴.
 *                             Kotlin 스코프 함수 → 직접 call → 메서드 전체 스캔 순서로 fallback.
 *
 * 4. 중복 제거
 *    (nodeId, usageKind, methodName, code) 키로 distinctBy 적용.
 *    여러 케이스가 동일 노드를 중복 탐지하는 경우를 걸러낸다.
 *
 * ── 설계 철학 ──────────────────────────────────────────────────────────────────
 *
 * 블랙리스트 방식을 채택한다: 명시적으로 제외하지 않는 한 사용처로 포함시킨다.
 * 과거 화이트리스트 방식(set/get/listener만 허용)은 requestFocus, show, hide 등을
 * 누락하는 문제가 있었다.
 * 누락(false negative)을 최소화하는 것을 우선하고, 잡음(false positive)은
 * PostProcessor(dedup/filter)에서 후처리한다.
 *
 * ── 한계 ───────────────────────────────────────────────────────────────────────
 *
 * 구조적으로 탐지가 어려운 케이스:
 *   - 컬렉션/맵에 저장 후 꺼내 쓰는 간접 흐름
 *   - 2-hop 이상의 inter-procedural 전달
 *   - Reflection, dynamic dispatch
 *   - 스코프 함수 결과가 다시 체이닝되는 복합 패턴
 */
object AnchorUsagesSemanticUsageDetector {
  import AnchorUsagesModel.*

  private def classifyDirectCallAsUsages(anchor: ViewAnchor, call: Call): List[SemanticUsage] = {
    val byCallName = AnchorUsagesCaseSupport.classifyCallAsUsages(anchor, call)
    if (byCallName.nonEmpty) byCallName
    else {
      val code = Option(call.code).getOrElse("")
      AnchorUsagesUiSignals.classifyCallName(code)
        .map(sig => AnchorUsagesCaseSupport.buildUsage(anchor, AnchorUsagesUiSignals.usageKindOf(sig), call))
        .toList
    }
  }

  def detectSemanticUsage(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    // SCOPE anchors: the scope Call itself is the single Usage Point (whole block = 1 usage).
    if (anchor.usageType == "SCOPE") {
      return node match {
        case scopeCall: Call => List(AnchorUsagesCaseSupport.buildScopeUsage(scopeCall))
        case _               => Nil
      }
    }

    if (anchor.usageType == "DIRECT_USAGE") {
      return node match {
        case directCall: Call =>
          val chainingAnchor = anchor.copy(usageType = "CHAINING")
          val closureCapture =
            AnchorUsagesClosureCaptureCase.detectOnCall(chainingAnchor, node, directCall).toList
          if (closureCapture.nonEmpty) {
            closureCapture
          } else if (Option(directCall.name).contains("<operator>.assignment")) {
            List(AnchorUsagesCaseSupport.buildUsage(anchor, UsageKind.Setter, directCall))
          } else {
            val fieldInstance = AnchorUsagesFieldInstanceCase.detect(anchor, node)
            if (fieldInstance.nonEmpty) fieldInstance
            else classifyDirectCallAsUsages(anchor, directCall)
          }
        case _ =>
          Nil
      }
    }

    val chaining      = AnchorUsagesChainingCase.detect(anchor, node)
    val passThrough   = AnchorUsagesPassThroughCase.detect(anchor, node)
    val closureCapture = AnchorUsagesClosureCaptureCase.detect(anchor, node)
    val fieldInstance = AnchorUsagesFieldInstanceCase.detect(anchor, node)
    val reference     = AnchorUsagesReceiverCase.detect(anchor, node)

    (chaining ++ passThrough ++ closureCapture ++ fieldInstance ++ reference)
      .distinctBy { usage =>
        val methodName = Option(usage.usageMethodFullName).filter(_.nonEmpty).getOrElse(usage.methodFullName)
        (usage.nodeId, usage.usageKind.toString, methodName, Option(usage.code).getOrElse(""))
      }
  }
}
