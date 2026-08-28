object AnchorUsagesUiSignals {
  import AnchorUsagesModel.*

  enum UiSignalKind {
    case Getter, Setter, Listener, Other
  }

  case class UiSignal(kind: UiSignalKind, key: String)

  // ========== BLACKLIST: 명백히 UI 상태와 무관한 것들 ==========
  
  private val internalLogicBlacklist = Set(
    // 로깅
    "log", "println", "print", "debug", "info", "warn", "error",
    // 문자열 처리
    "tostring", "format", "contains", "equals", "split", "replace", "trim", "substring",
    // 컬렉션 조회 (변경 아님)
    "size", "length", "isempty", "indexof", "get", "containskey", "containsvalue",
    // 타입 변환
    "toint", "todouble", "toboolean", "tolist", "toset", "tomap",
    // Object 기본 메서드
    "hashcode", "clone", "wait", "notify", "notifyall",
    // Android internal (앱 로직 아님)
    "getcontext", "getresources", "getpackagename", "getapplication", "getactivity",
    // 시간/날짜 조회
    "getcurrenttime", "gettime", "gettimeinmillis", "now",
    // 검증 메서드
    "validate", "check", "verify", "require", "assert",
    // ㅇㅇㅇ
    "drawerbackgrounds", "elevation", "minimumheight", "transitionname", "addsharedelement", "colorfilter", "alpha",
    "setlive", "setcolor", "settextcolor", "settextsize", "animate", "textalignment", "imagealpha"
  )

  private val ignoredPrefixes = Set(
    "on",       // 콜백 메서드 (호출하는 게 아니라 오버라이드 받는 것)
    "init",     // 초기화
    "create",   // 객체 생성 (UI 상태 변경 아님)
    "build",    // 빌더 패턴
    "calculate", "compute",  // 계산
    "fetch", "load",  // 데이터 로드 (UI 변경은 그 이후)
    "parse", "decode", "encode"  // 데이터 변환
  )

  // 명백히 UI와 무관한 것
  def isBlacklisted(name: String): Boolean = {
    val lower = name.toLowerCase
    
    // 직접 매칭
    if (internalLogicBlacklist.contains(lower)) return true
    
    // Prefix 매칭
    if (ignoredPrefixes.exists(lower.startsWith)) return true
    
    false
  }

  // ========== 포괄적 분류 (Blacklist 제외하고 모두 포함) ==========
  
  /*
   * 기존 Whitelist 방식 참고 (현재는 사용 안 함):
   * 
   * Listener 감지:
   *   - lower.startsWith("seton")
   *   - lower.contains("addtextchangedlistener")
   *   - lower.contains("setoncheckedchangelistener")
   *   - lower.contains("setontouchlistener")
   *   - lower.contains("setonlongclicklistener")
   * 
   * Setter 감지:
   *   - lower.startsWith("set")
   * 
   * Getter 감지:
   *   - lower.startsWith("get") || lower.startsWith("is")
   * 
   * 문제점:
   *   - requestFocus, dismiss, show, hide, add, remove 등 누락
   *   - 알 수 없는 메서드는 모두 제외 (과도한 필터링)
   */
  
  def classifyCallName(name: String): Option[UiSignal] = {
    val raw = Option(name).getOrElse("")
    if (raw.isEmpty) return None
    
    val lower = raw.toLowerCase
    
    // Blacklist 체크
    if (isBlacklisted(lower)) return None
    
    // Listener 패턴 (이벤트 등록)
    if (lower.contains("listener") || lower.startsWith("seton") || lower.startsWith("addon")) {
      return Some(UiSignal(UiSignalKind.Listener, raw))
    }
    
    // Setter 패턴 (set으로 시작하는 모든 메서드)
    if (lower.startsWith("set")) {
      return Some(UiSignal(UiSignalKind.Setter, raw))
    }
    
    // Getter 패턴 (get, is, has로 시작)
    if (lower.startsWith("get") || lower.startsWith("is") || lower.startsWith("has")) {
      return Some(UiSignal(UiSignalKind.Getter, raw))
    }
    
    // 동사 패턴 (add, remove, show, hide, dismiss, request 등 - UI 변경 동사)
    // 기존에는 누락되었던 패턴들!
    if (isUiModifierVerb(lower)) {
      return Some(UiSignal(UiSignalKind.Setter, raw))  // Setter로 취급
    }
    
    // 알 수 없지만 blacklist 아니므로 일단 포함
    Some(UiSignal(UiSignalKind.Other, raw))
  }

  private def isUiModifierVerb(lower: String): Boolean = {
    Set(
      "add", "remove", "clear", "delete", "insert",  // 추가/제거
      "show", "hide", "dismiss", "close", "open",    // 가시성
      "collapse", "expand", "toggle",                // 상태 전환
      "request", "perform", "post", "send",          // 액션 트리거
      "invalidate", "refresh", "update", "redraw",   // 갱신
      "animate", "start", "stop", "pause", "resume"  // 애니메이션/제어
    ).exists(lower.startsWith)
  }

  def classifyPropertyAccess(propNameOpt: Option[String], isWrite: Boolean): UiSignal = {
    val propName = propNameOpt.getOrElse("unknown")
    
    // Blacklist 체크
    if (isBlacklisted(propName)) {
      return UiSignal(UiSignalKind.Other, propName)
    }
    
    val kind = if (isWrite) UiSignalKind.Setter else UiSignalKind.Getter
    UiSignal(kind, propName)
  }

  def usageKindOf(sig: UiSignal): UsageKind =
    sig.kind match {
      case UiSignalKind.Getter => UsageKind.Getter
      case UiSignalKind.Setter => UsageKind.Setter
      case UiSignalKind.Listener => UsageKind.Listener
      case UiSignalKind.Other => UsageKind.Other
    }

}
