import io.shiftleft.codepropertygraph.generated.nodes.*

/** Shared data model for AnchorUsages pipeline */
object AnchorUsagesModel {
  type ViewAnchor = ViewAnchorContract.ViewAnchor
  val ViewAnchor: ViewAnchorContract.ViewAnchor.type = ViewAnchorContract.ViewAnchor

  // UI 상호작용 관점의 사용 유형 분류
  enum UsageKind {
    case Getter    // 상태 읽기: isEnabled, getText(), isChecked
    case Setter    // 상태 쓰기: setEnabled(), setText(), visibility =
    case Listener  // 이벤트 리스너 등록: setOnClickListener, addXxxListener
    case Delegate  // 다른 메서드로 View를 위임: initView(btn), RETURN 앵커 caller
    case Other     // 분류 불가 or Action: requestFocus, dismiss, performClick

    def outputLabel: String = this match {
      case Getter   => "GETTER"
      case Setter   => "SETTER"
      case Listener => "LISTENER"
      case Delegate => "DELEGATE"
      case Other    => "OTHER"
    }
  }

  // 파일 경로 + 한 줄 번호를 하나의 위치로 묶는다.
  case class SourceLocation(
    file: String,
    line: Int
  ) {
    def toDisplayString: String = {
      val normalizedFile = SourceLocation.normalizeFile(file)
      s"$normalizedFile:$line"
    }
  }

  object SourceLocation {
    private val UnknownFile = "?"

    private def normalizeFile(rawFile: String): String = {
      val trimmed = Option(rawFile).getOrElse("").trim
      if (trimmed.isEmpty) UnknownFile else trimmed
    }

    def fromFileLine(file: String, line: Int): SourceLocation =
      SourceLocation(normalizeFile(file), line)

    def fromString(rawLocation: String): SourceLocation = {
      val raw = Option(rawLocation).getOrElse("")
      if (raw.trim.isEmpty) return SourceLocation(UnknownFile, -1)

      val idx = raw.lastIndexOf(':')
      if (idx <= 0 || idx >= raw.length - 1) {
        SourceLocation(normalizeFile(raw), -1)
      } else {
        val file = raw.substring(0, idx)
        val line = raw.substring(idx + 1).toIntOption.getOrElse(-1)
        SourceLocation(normalizeFile(file), line)
      }
    }
  }

  // ASSIGNMENT 앵커용 선언⋅참조 해석기가 찾은 한 개의 CPG 참조 지점을 담는 클래스 
  case class DeclarationReference(
    nodeId: Long,
    nodeLabel: String,
    code: String,
    location: String,
    methodFullName: String
  )

  // 한 개의 ViewAnchor에 대해 존재하는 (AssignmentDeclAndRefResolver가 모은) 선언(Declaration)별 참조 목록을 담는 클래스
  case class AnchorDeclaration(
    anchor: ViewAnchor,
    declarations: List[(Declaration, List[DeclarationReference])]
  )

  // 내부 처리 모델: Usage Point 탐지 결과 (semantic usage 탐지기가 만든 내부 중간 결과)
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

  case class UsageReport(
    anchor: ViewAnchor,
    usages: List[SemanticUsage]
  )

  // Output schema (anchor-usages.json의 단일 진실 공급원)
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

  // 패키지 경로만 있는 코드 노이즈 판별 (e.g. "com.example.ui.AlarmFragment")
  // PostProcessor와 Json 직렬화 양쪽에서 공유
  def isPackagePathOnly(code: String): Boolean = {
    val trimmed = Option(code).getOrElse("").trim
    if (trimmed.isEmpty) return false
    val hasDelimiterChars =
      trimmed.exists(ch => "()=+-*/{}[],:\"'`".contains(ch)) || trimmed.exists(_.isWhitespace)
    if (hasDelimiterChars) return false
    trimmed.matches("^[a-z][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*){2,}$")
  }
}
