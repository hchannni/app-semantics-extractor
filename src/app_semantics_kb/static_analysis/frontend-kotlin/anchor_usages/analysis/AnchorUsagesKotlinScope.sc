import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

import scala.annotation.tailrec

/**
 * Kotlin receiver-lambda 스코프 함수(run/apply/with) 블록 내부에서
 * 앵커에 대해 이루어지는 UI 조작을 개별 SemanticUsage로 추출하는 모듈.
 * ── 호출 맥락 ──────────────────────────────────────────────────────────────────
 * ReceiverCase.scopeUsages에서 앵커 참조의 조상 중 스코프 함수 Call이 있을 때 호출된다.
 * SemanticUsageDetector의 SCOPE early-exit(블록 전체 = 1 usage)과는 다른 경로이다.
 *   - SCOPE early-exit : usageType == "SCOPE" 앵커 (lookup Call 자체가 스코프 안에 있는 경우)
 *   - 이 모듈        : usageType == "ASSIGNMENT" 앵커가 스코프 함수 블록 안에서 참조될 때
 *                      → 블록 안의 각 UI 조작마다 개별 SemanticUsage를 생성한다.
 *
 * ── scopeReceiverSignalUsages 동작 ─────────────────────────────────────────────
 *
 * 1. 전제 조건 검사 (2단계 gate)
 *    (a) scopeCall 이름/fullName에 run/apply/with가 포함되는지 확인. 아니면 Nil 반환.
 *    (b) anchorRef가 scopeCall의 receiver 인자 서브트리 안에 실제로 포함되는지 확인. 없으면 Nil 반환.
 *
 * 2. 블록 내 Call 수집 (우선순위 순)
 *    MethodRef가 있으면 → 참조된 람다 메서드 body의 Call들
 *    없으면             → 인라인 Block 하위의 Call들
 *    둘 다 없으면       → scopeCall.ast 전체 fallback
 *
 * 3. 블록 안에서 잡는 3가지 패턴
 *    (1) 프로퍼티 할당 (<operator>.assignment)
 *        (A) LHS가 Identifier                   → classifyPropertyAccess(isWrite=true) → Setter
 *        (B) LHS가 this.<prop> 형태의 fieldAccess → 동일하게 Setter
 *    (2) 프로퍼티 읽기 (<operator>.fieldAccess)
 *        receiver가 implicit this이면           → classifyPropertyAccess(isWrite=false) → Getter
 *    (3) 메서드 호출 (implicit this 또는 receiver 없는 unqualified 호출)
 *        setOnClickListener(), requestFocus() 등 → classifyCallName으로 분류
 *
 * ── 핵심 설계 포인트 ────────────────────────────────────────────────────────────
 *
 * isImplicitThis: Kotlin 컴파일러가 receiver를 $this$apply 형태로 노출하므로
 *                 이름/코드에 "this$" 패턴이 있으면 implicit this로 판별한다.
 * receiver 없는 call 포함: Kotlin receiver-lambda 안에서는 receiver 없이 메서드를
 *                          직접 호출하는 것이 일반적이라 recvOpt.isEmpty도 허용한다.
 */
object AnchorUsagesKotlinScope {
  import AnchorUsagesUiSignals.*
  import AnchorUsagesModel.*

  private def isImplicitThis(n: AstNode): Boolean =
    n match {
      case id: Identifier =>
        val name = Option(id.name).getOrElse("")
        val code = Option(id.code).getOrElse("")
        name == "this" || code == "this" ||
          name.startsWith("$this$") || code.startsWith("$this$") ||
          name.contains("this$") || code.contains("this$")
      case _ =>
        val code = Option(n.code).getOrElse("")
        code == "this" || code.startsWith("$this$") || code.contains("this$")
    }

  private def receiverArgInScopeCall(scopeCall: Call, anchorRef: AstNode): Option[AstNode] =
    // In Kotlin, receiver-lambda scope calls (run/apply/with) are sometimes modeled such that
    // the receiver is an AST child but not a Call "argument". Consider both.
    (scopeCall.argument.collect { case a: AstNode => a }.l ++ scopeCall.astChildren.collect { case a: AstNode => a }.l)
      .find(arg => AnchorUsagesCaseSupport.subtreeContains(arg, anchorRef.id))

  private def callsFromMethodRefs(scopeCall: Call)(implicit cpg: Cpg): List[Call] = {
    val mrefs = (scopeCall.argument.collectAll[MethodRef].l ++ scopeCall.astChildren.collectAll[MethodRef].l).distinctBy(_.id)
    val methods =
      mrefs.flatMap { mr =>
        val mf = Option(mr.methodFullName).getOrElse("")
        if (mf.isEmpty) Nil
        else cpg.method.filter(m => Option(m.methodFullName).contains(mf)).l
      }
    methods.distinctBy(_.id).flatMap { m =>
      Option(m.block).toList.flatMap(_.ast.collectAll[Call].l)
    }
  }

  private def callsFromBlocks(scopeCall: Call): List[Call] = {
    val blocks = scopeCall.argument.collectAll[Block].l ++ scopeCall.astChildren.collectAll[Block].l
    blocks.flatMap(_.ast.collectAll[Call].l)
  }

  private def callsFromScopeAstFallback(scopeCall: Call): List[Call] =
    scopeCall.ast.collectAll[Call].l.filterNot(_.id == scopeCall.id)

  // Exposed: infer UI signals inside Kotlin receiver-lambda scope functions (run/apply/with)
  def scopeReceiverSignalUsages(
    anchor: ViewAnchor,
    anchorRef: AstNode,
    scopeCall: Call,
    buildUsage: (UsageKind, Call) => SemanticUsage
  )(implicit cpg: Cpg): List[SemanticUsage] = {
    val ident =
      (Option(scopeCall.name).getOrElse("") + " " + Option(scopeCall.methodFullName).getOrElse(""))
        .toLowerCase
    val isReceiverLambda = ident.contains("run") || ident.contains("apply") || ident.contains("with")
    if (!isReceiverLambda) return Nil

    if (receiverArgInScopeCall(scopeCall, anchorRef).isEmpty) return Nil

    val callsInScopeBody = {
      val fromMr = callsFromMethodRefs(scopeCall)
      val base = if (fromMr.nonEmpty) fromMr else callsFromBlocks(scopeCall)
      if (base.nonEmpty) base else callsFromScopeAstFallback(scopeCall)
    }

    val out = scala.collection.mutable.ListBuffer[SemanticUsage]()

    // 1) assignment: text = ..., visibility = ...
    callsInScopeBody
      .filter(c => Option(c.name).contains("<operator>.assignment"))
      .foreach { assign =>
        // (A) LHS Identifier: text = ...
        assign.argumentOption(1).collect { case id: Identifier => id }.foreach { lhsId =>
          val sig = classifyPropertyAccess(Option(lhsId.name), isWrite = true)
          out += buildUsage(usageKindOf(sig), assign)
        }

        // (B) LHS this.<prop>
        assign.argumentOption(1).collect { case c: Call if c.name == "<operator>.fieldAccess" => c }.foreach { lhsFa =>
          val recvOpt = lhsFa.argumentOption(1).collect { case a: AstNode => a }
          val propOpt = lhsFa.argument(2).collect { case fi: FieldIdentifier => fi.canonicalName }.headOption
          if (recvOpt.exists(isImplicitThis)) {
            val sig = classifyPropertyAccess(propOpt, isWrite = true)
            out += buildUsage(usageKindOf(sig), assign)
          }
        }
      }

    // 2) property read: this.<prop>
    callsInScopeBody
      .filter(c => Option(c.name).contains("<operator>.fieldAccess"))
      .foreach { fa =>
        val recvOpt = fa.argumentOption(1).collect { case a: AstNode => a }
        val propOpt = fa.argument(2).collect { case fi: FieldIdentifier => fi.canonicalName }.headOption
        if (recvOpt.exists(isImplicitThis)) {
          val sig = classifyPropertyAccess(propOpt, isWrite = false)
          out += buildUsage(usageKindOf(sig), fa)
        }
      }

    // 3) method calls on implicit this: setText(...), isChecked(), ...
    callsInScopeBody.foreach { call =>
      val recvOpt = call.argumentOption(0).collect { case a: AstNode => a }
      // In Kotlin receiver-lambda bodies, many calls are unqualified and may not have an explicit receiver arg.
      // Treat missing receiver as implicit-this, but keep signal filtering policy unchanged.
      if (recvOpt.exists(isImplicitThis) || recvOpt.isEmpty) {
        classifyCallName(call.name).foreach { sig =>
          out += buildUsage(usageKindOf(sig), call)
        }
      }
    }

    out.toList
  }
}
