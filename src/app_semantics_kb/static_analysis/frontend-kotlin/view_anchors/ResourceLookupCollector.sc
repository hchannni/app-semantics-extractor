import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.semanticcpg.language.*

import ViewAnchorContract.ViewAnchor

object ResourceLookupCollector {
  /**
    * Resource ID를 신뢰 소스로 삼아 lookup anchor를 수집한다.
    * 1) carrier(`R.id.*` direct/local alias) 확인
    * 2) exact lookup 또는 검증된 wrapper lookup 판정
    * 3) hybrid UI type gate fallback 적용
    */
  def collect()(implicit cpg: Cpg): List[ViewAnchor] = {
    val wrapperMethodFullNames = WrapperLookupDiscovery.discoverWrapperMethods()

    cpg.call
      .filter(call => ResourceIdCarrierResolver.callHasResourceIdArgument(call))
      .filter { call =>
        // exact/wrapper lookup이면 바로 채택하고, 아니면 UI-like type gate에서만 fallback 허용.
        ResourceLookupRules.isResourceLookupCall(call, wrapperMethodFullNames) ||
          ViewAnchorBuilder.isUiLikeLookupCandidate(call)
      }
      .flatMap { call =>
        ResourceIdCarrierResolver.resourceIdFromCallArguments(call).map { resourceId =>
          ViewAnchorBuilder.buildAnchorFromCall(call, resourceId)
        }
      }
      .l
  }
}
