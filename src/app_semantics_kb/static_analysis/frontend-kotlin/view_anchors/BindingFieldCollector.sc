import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.semanticcpg.language.*

import ViewAnchorContract.ViewAnchor

object BindingFieldCollector {
  def collect()(implicit cpg: Cpg): List[ViewAnchor] =
    cpg.call
      .filter(BindingFieldRules.isBindingFieldAccess)
      .flatMap { call =>
        BindingFieldRules.bindingFieldNameFrom(call).map { fieldName =>
          val resourceId = s"R.id.${BindingFieldRules.camelToSnake(fieldName)}"
          ViewAnchorBuilder.buildAnchorFromCall(call, resourceId)
        }
      }
      .l
}
