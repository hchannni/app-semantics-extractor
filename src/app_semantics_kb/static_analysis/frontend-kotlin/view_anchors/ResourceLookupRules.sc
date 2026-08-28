import io.shiftleft.codepropertygraph.generated.nodes.Call
import io.shiftleft.semanticcpg.language.*

object ResourceLookupRules {
  private val resourcePattern = """[\w\.]*R(?:\d+)?\.id\.[\w_]+""".r
  private val exactLookupNames = Set(
    "findviewbyid",
    "findfragmentbyid",
    "finditem",
    "findbyid",
    "requireviewbyid",
    "inflate"
  )

  /** Method fullName에서 short method name만 추출해 exact lookup 판별에 사용한다. */
  private def shortNameFromMethodFullName(methodFullName: String): Option[String] = {
    val beforeSignature = methodFullName.takeWhile(_ != ':')
    val shortName = beforeSignature.split('.').lastOption.getOrElse("")
    Option(shortName).filter(_.nonEmpty)
  }

  /** 호출명/메서드명 비교를 위한 소문자 정규화 헬퍼. */
  private def normalizedCallName(call: Call): String =
    Option(call.name).getOrElse("").trim.toLowerCase

  /** methodFullName에서도 short name을 추출해 exact 비교에 사용한다. */
  private def normalizedFullNameShort(call: Call): Option[String] =
    Option(call.methodFullName)
      .flatMap(shortNameFromMethodFullName)
      .map(_.toLowerCase)

  /** 인자 코드에서 직접 `R.id.*`를 추출한다 (직접 전달 케이스). */
  def resourceIdFrom(call: Call): Option[String] =
    call.argument
      .flatMap(arg => Option(arg.code))
      .flatMap(code => resourcePattern.findFirstIn(code))
      .headOption

  /** 인자 중 `R.id.*`가 직접 존재하는지 빠르게 확인한다. */
  def hasResourceArgument(call: Call): Boolean =
    call.argument
      .flatMap(arg => Option(arg.code))
      .exists(code => resourcePattern.findFirstIn(code).nonEmpty)

  /** broad contains 매칭 대신 exact method name으로 lookup call을 판별한다. */
  def isExactLookupCall(call: Call): Boolean =
    exactLookupNames.contains(normalizedCallName(call)) ||
      normalizedFullNameShort(call).exists(exactLookupNames.contains)

  /** wrapper discovery 결과를 이용해 lookup wrapper call 여부를 판별한다. */
  def isWrapperLookupCall(call: Call, wrapperMethodFullNames: Set[String]): Boolean =
    Option(call.methodFullName).exists(wrapperMethodFullNames.contains)

  /** Resource lookup call의 최종 판정: exact lookup 또는 검증된 wrapper call. */
  def isResourceLookupCall(call: Call, wrapperMethodFullNames: Set[String]): Boolean =
    isExactLookupCall(call) || isWrapperLookupCall(call, wrapperMethodFullNames)

}
