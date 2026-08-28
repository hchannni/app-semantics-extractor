import io.shiftleft.codepropertygraph.generated.nodes.*

object JavaAnchorUsagesModel {
  type ViewAnchor = JavaViewAnchorModel.JavaViewAnchor
  val ViewAnchor = JavaViewAnchorModel.JavaViewAnchor

  enum UsageKind {
    case Getter
    case Setter
    case Listener
    case Delegate
    case Other

    def outputLabel: String = this match {
      case Getter => "GETTER"
      case Setter => "SETTER"
      case Listener => "LISTENER"
      case Delegate => "DELEGATE"
      case Other => "OTHER"
    }
  }

  case class SourceLocation(file: String, line: Int) {
    def toDisplayString: String = s"$file:$line"
  }

  object SourceLocation {
    def fromString(raw: String): SourceLocation = {
      val value = Option(raw).getOrElse("")
      val idx = value.lastIndexOf(':')
      if (idx <= 0 || idx >= value.length - 1) SourceLocation(value, -1)
      else SourceLocation(value.substring(0, idx), value.substring(idx + 1).toIntOption.getOrElse(-1))
    }
  }

  case class DeclarationReference(
    nodeId: Long,
    nodeLabel: String,
    code: String,
    location: String,
    methodFullName: String
  )

  case class AnchorDeclaration(
    anchor: ViewAnchor,
    declarations: List[(Declaration, List[DeclarationReference])]
  )

  case class SemanticUsage(
    usageKind: UsageKind,
    nodeLabel: String,
    code: String,
    sourceLocation: SourceLocation,
    methodFullName: String,
    nodeId: Long,
    usageMethodFullName: String = ""
  ) {
    def location: String = sourceLocation.toDisplayString
  }

  case class UsageReport(anchor: ViewAnchor, usages: List[SemanticUsage])

  case class UsagePoint(
    nodeId: Long,
    nodeLabel: String,
    file: String,
    startLine: Int,
    endLine: Int,
    code: String,
    usageKind: String
  )

  case class AnchorUsage(
    anchorId: String,
    usagePoint: UsagePoint,
    enclosingMethodFullName: String
  )

  def isPackagePathOnly(code: String): Boolean = {
    val trimmed = Option(code).getOrElse("").trim
    if (trimmed.isEmpty) return false
    val hasDelimiterChars =
      trimmed.exists(ch => "()=+-*/{}[],:\"'`".contains(ch)) || trimmed.exists(_.isWhitespace)
    if (hasDelimiterChars) return false
    trimmed.matches("^[a-z][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*){2,}$")
  }
}
