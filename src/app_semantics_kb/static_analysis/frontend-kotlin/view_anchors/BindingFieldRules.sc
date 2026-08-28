import io.shiftleft.codepropertygraph.generated.nodes.{Call, FieldIdentifier}
import io.shiftleft.semanticcpg.language.*

object BindingFieldRules {
  private val bindingFieldAccessCodePattern = """(?:^|[^A-Za-z0-9_])binding\.([A-Za-z][A-Za-z0-9_]*)""".r

  def camelToSnake(name: String): String =
    name.zipWithIndex
      .flatMap { case (ch, idx) =>
        if (ch.isUpper) {
          if (idx == 0) Seq(ch.toLower)
          else Seq('_', ch.toLower)
        } else Seq(ch)
      }
      .mkString

  def isBindingFieldAccess(call: Call): Boolean =
    Option(call.name).contains("<operator>.fieldAccess") &&
      Option(call.code).exists(code => bindingFieldAccessCodePattern.findFirstMatchIn(code).nonEmpty)

  def bindingFieldNameFrom(call: Call): Option[String] =
    call.argument(2)    // E.g., binding.foo 에서 foo 추출
      .collect { case field: FieldIdentifier => Option(field.canonicalName).orElse(Option(field.code)) }
      .headOption
      .flatten
      .filter(_.nonEmpty)
      .orElse {
        Option(call.code).flatMap(code => bindingFieldAccessCodePattern.findFirstMatchIn(code).map(_.group(1)))
      }
}
