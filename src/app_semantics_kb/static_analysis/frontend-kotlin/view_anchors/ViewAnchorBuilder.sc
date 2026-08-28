import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

import ViewAnchorContract.ViewAnchor

object ViewAnchorBuilder {
  private val genericTypePattern = """find\w*<([\w\.]+)>""".r
  private val uiTypePrefixes = Seq(
    "android.view.",
    "android.widget.",
    "androidx.fragment.app.",
    "android.app."
  )

  /** Joern property 값을 안전하게 타입 문자열로 변환한다. */
  private def asTypeString(value: AnyRef): Option[String] =
    value match {
      case s: String if s.nonEmpty && s != "ANY" => Some(s)
      case _ => None
    }

  /**
    * call의 view/widget 타입을 추론한다.
    * 우선순위: generic type -> assignment target type -> node property type.
    */
  private def inferredViewType(call: Call): String = {
    val fromGeneric = Option(call.code)
      .flatMap(code => genericTypePattern.findFirstMatchIn(code).map(_.group(1)))

    val fromAssignment = call.inAssignment.target
      .collect { case id: Identifier => id.typeFullName }
      .filter(t => t != null && t != "ANY")
      .headOption

    val fromNode = asTypeString(call.propertyOption("TYPE_FULL_NAME").orNull)
      .orElse(asTypeString(call.propertyOption("TYPE_FULL_NAME_FULL").orNull))

    fromGeneric.orElse(fromAssignment).orElse(fromNode).getOrElse("UNKNOWN")
  }

  /** hybrid gate에서 사용하는 UI-like 후보 판정 함수다. */
  def isUiLikeLookupCandidate(call: Call): Boolean = {
    val normalizedType = inferredViewType(call).toLowerCase
    uiTypePrefixes.exists(prefix => normalizedType.startsWith(prefix)) ||
      normalizedType.contains("fragment")
  }

  /** usage target 노드를 anchor 이름으로 변환한다. */
  private def anchorNameFrom(targetNode: Option[AstNode]): Option[String] =
    targetNode match {
      case Some(identifier: Identifier) => Some(identifier.name)
      case Some(field: FieldIdentifier) => Some(field.canonicalName)
      case Some(parentCall: Call) => Option(parentCall.code)
      case _ => None
    }

  /**
    * call 노드에서 최종 ViewAnchor를 생성한다.
    * RETURN 케이스는 anchor_name이 비지 않도록 call.code를 fallback으로 보정한다.
    */
  def buildAnchorFromCall(
    call: Call,
    resourceId: String
  )(implicit cpg: Cpg): ViewAnchor = {
    val usage = ViewAnchorUsageAnalyzer.analyzeUsage(call)
    val viewType = inferredViewType(call)
    val location = s"${call.filename}:${call.lineNumber.getOrElse(-1)}"
    val targetName = anchorNameFrom(usage.targetNode)
    val normalizedAnchorName =
      if (usage.usageType == "RETURN") targetName.orElse(Option(call.code))
      else targetName

    val (cpgNodeId, cpgNodeType) = usage.targetNode match {
      case Some(n) => (n.id, n.label)
      case None    => (call.id, "CALL")
    }

    ViewAnchor(
      viewType = viewType,
      resourceId = resourceId,
      usageType = usage.usageType,
      cpgNodeId = cpgNodeId,
      cpgNodeType = cpgNodeType,
      anchorName = normalizedAnchorName,
      location = location,
      code = call.code,
      declarationScope = usage.declarationScope
    )
  }
}
