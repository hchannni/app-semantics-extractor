import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

object WrapperLookupDiscovery {
  private val defaultMaxDepth = 4

  /** wrapper call 후보가 known lookup/wrapper를 호출하는지 확인한다. */
  private def callsKnownLookupOrWrapper(
    call: Call,
    knownLookupMethodFullNames: Set[String],
    knownWrapperMethodFullNames: Set[String]
  ): Boolean =
    Option(call.methodFullName).exists { callee =>
      knownLookupMethodFullNames.contains(callee) || knownWrapperMethodFullNames.contains(callee)
    }

  /** wrapper 판정: 호출 인자에 현재 메서드의 parameter가 pass-through 되는지 확인한다. */
  private def passesMethodParameter(call: Call): Boolean =
    call.argument
      .collect { case identifier: Identifier => identifier }
      .exists { identifier =>
        // CPGQL: IDENTIFIER -> REF -> METHOD_PARAMETER_IN 경로가 있으면 parameter forwarding으로 간주.
        identifier.refsTo.collect { case _: MethodParameterIn => true }.nonEmpty
      }

  /**
    * exact lookup 호출을 감싸는 wrapper 메서드를 bounded fixpoint로 탐색한다.
    * depth 제한과 고정점 종료를 함께 사용해 대형 앱에서의 경로 폭발을 방지한다.
    */
  def discoverWrapperMethods(maxDepth: Int = defaultMaxDepth)(implicit cpg: Cpg): Set[String] = {
    val knownLookupMethodFullNames =
      cpg.call
        .filter(ResourceLookupRules.isExactLookupCall)
        .flatMap(call => Option(call.methodFullName))
        .toSet

    var knownWrapperMethodFullNames = Set.empty[String]
    var depth = 0

    while (depth < maxDepth) {
      val newlyDiscovered =
        cpg.method
          .flatMap { method =>
            val methodFullName = Option(method.fullName).getOrElse("")
            if (methodFullName.isEmpty || knownWrapperMethodFullNames.contains(methodFullName)) Nil
            else {
              val isWrapper =
                method.call.l.exists { call =>
                  callsKnownLookupOrWrapper(call, knownLookupMethodFullNames, knownWrapperMethodFullNames) &&
                    passesMethodParameter(call)
                }
              if (isWrapper) List(methodFullName) else Nil
            }
          }
          .toSet -- knownWrapperMethodFullNames

      if (newlyDiscovered.isEmpty) return knownWrapperMethodFullNames

      knownWrapperMethodFullNames ++= newlyDiscovered
      depth += 1
    }

    knownWrapperMethodFullNames
  }
}
