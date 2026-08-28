import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

/**
 * ViewAnchor의 usageType에 따라 semantic usage 탐색을 시작할 CPG 노드들을 결정한다.
 *
 * usageType별 전략:
 * - ASSIGNMENT — AssignmentDeclAndRefResolver가 수집한 앵커 변수/필드의 모든 참조 nodeId를 CPG에서 로드한다.
 * - CHAINING   — cpgNodeId가 source-level view handle/acquisition call을 가리키므로, 그대로 로드해 반환한다.
 *                listener/setter 같은 parent semantic operation은 SemanticUsageDetector의 receiver/closure case가 찾는다.
 * - DIRECT_USAGE — V2 occurrence가 이미 listener/setter 같은 semantic operation call을 가리키므로, 그대로 로드해 반환한다.
 * - SCOPE      — cpgNodeId가 이미 scope 함수 call 노드(run/apply 등)를 가리키므로, 그대로 로드해 반환한다.
 * - RETURN     — callerCallSitesOf (직접 caller call site)와 lazyPropertyFallbackOf
 *                (outer method의 Local 참조 Identifier)를 항상 병합해 반환한다.
 *                by-lazy 패턴은 fallback으로, 일반 RETURN 패턴은 callerCallSitesOf로 처리된다.
 * - 그 외       — 빈 리스트.
 *
 * 탐색 전략 함수(loadNode, isScopeFunction, callerCallSitesOf, lazyPropertyFallbackOf)는
 * 호출부(AnchorUsagesAnalysis)에서 주입하므로, 이 객체는 usageType 분기 로직만 담는다.
 */
object AnchorUsagesTargetResolver {
  import AnchorUsagesModel.*

  def targetNodesFor(
    anchor: ViewAnchor,
    declarations: Option[AnchorDeclaration],
    loadNode: Long => Option[AstNode],
    isScopeFunction: Call => Boolean,
    callerCallSitesOf: AstNode => List[AstNode],
    lazyPropertyFallbackOf: (AstNode, String) => List[AstNode]
  )(implicit cpg: Cpg): List[AstNode] =
    anchor.usageType match {
      case "ASSIGNMENT" =>
        declarations.toList
          .flatMap(_.declarations)
          .flatMap { case (_, refs) => refs.map(_.nodeId) }
          .flatMap(loadNode)
          .distinctBy(_.id)

      case "CHAINING" =>
        // cpgNodeId는 ViewAnchor가 저장한 handle/acquisition 노드 ID다.
        // parent semantic operation은 receiver/closure detector가 anchorRef 주변 문맥에서 찾는다.
        loadNode(anchor.cpgNodeId)
          .collect { case call: Call => call: AstNode }
          .toList

      case "DIRECT_USAGE" =>
        // V2 occurrence는 이미 사용 지점 자체를 가리킨다.
        loadNode(anchor.cpgNodeId).toList

      case "SCOPE" =>
        // cpgNodeId는 ViewAnchorBuilder가 저장한 scope call 노드 ID다.
        // 로드된 노드가 실제로 scope 함수인지 검증 후 반환한다.
        loadNode(anchor.cpgNodeId)
          .collect { case call: Call if isScopeFunction(call) => call }
          .toList
          .distinctBy(_.id)

      case "RETURN" =>
        // 두 전략을 병합: by-lazy와 일반 RETURN 모두 올바르게 처리하기 위해 항상 양쪽 실행.
        //  - callerCallSitesOf: enclosing method의 직접 caller call site (일반 RETURN 패턴용)
        //  - lazyPropertyFallbackOf: outer method의 Local 참조 Identifier (by-lazy 패턴용)
        // by-lazy에서 callerCallSitesOf가 outer method 호출부를 잘못 반환할 경우 노이즈가 되지만,
        // 이 노드들은 semantic use case detector에서 usage를 생성하지 않아 자연히 필터링된다.
        val directCallers = loadNode(anchor.cpgNodeId).toList
          .flatMap(callerCallSitesOf)
          .distinctBy(_.id)

        val lazyFallback = loadNode(anchor.cpgNodeId).toList
          .flatMap(node => lazyPropertyFallbackOf(node, anchor.viewType))
          .distinctBy(_.id)

        (directCallers ++ lazyFallback).distinctBy(_.id)

      case _ => Nil
    }
}
