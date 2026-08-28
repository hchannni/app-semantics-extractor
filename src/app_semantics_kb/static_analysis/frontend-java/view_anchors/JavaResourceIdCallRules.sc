import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

object JavaResourceIdCallRules {
  private val resourcePattern = """[\w\.]*R(?:\d+)?\.id\.[\w_]+""".r
  private val lookupNames = Set("findviewbyid", "requireviewbyid")

  private def shortNameFromMethodFullName(methodFullName: String): Option[String] = {
    val beforeSignature = methodFullName.takeWhile(_ != ':')
    Option(beforeSignature.split('.').lastOption.getOrElse("")).filter(_.nonEmpty)
  }

  def isJavaSource(node: AstNode): Boolean =
    Option(node.filename).exists(_.endsWith(".java"))

  def resourceIdFrom(call: Call): Option[String] = {
    val argumentCodes = call.argument.flatMap(arg => Option(arg.code)).l
    val candidateCodes = argumentCodes :+ Option(call.code).getOrElse("")
    candidateCodes.flatMap(code => resourcePattern.findFirstIn(code)).headOption
  }

  def isFindViewByIdCall(call: Call): Boolean = {
    val normalizedName = Option(call.name).getOrElse("").trim.toLowerCase
    val normalizedFullShort = Option(call.methodFullName)
      .flatMap(shortNameFromMethodFullName)
      .map(_.toLowerCase)
    lookupNames.contains(normalizedName) || normalizedFullShort.exists(lookupNames.contains)
  }

  def isJavaResourceLookupCall(call: Call): Boolean =
    isJavaSource(call) && isFindViewByIdCall(call) && resourceIdFrom(call).nonEmpty
}
