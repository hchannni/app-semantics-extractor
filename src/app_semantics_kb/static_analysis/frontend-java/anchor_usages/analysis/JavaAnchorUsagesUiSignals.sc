import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import scala.util.Try

object JavaAnchorUsagesUiSignals {
  import JavaAnchorUsagesModel.*

  private val listenerNames = Set(
    "setonclicklistener",
    "setonlongclicklistener",
    "setoncheckedchangelistener",
    "setoncancellistener",
    "addtextchangedlistener"
  )

  private val ignoredNames = Set(
    "tostring",
    "format",
    "contains",
    "equals",
    "hashcode",
    "println",
    "print"
  )
  private val setterPrefixes = Seq("set", "update", "add", "remove", "request", "show", "hide", "dismiss", "clear")
  private val getterPrefixes = Seq("get", "is", "has")
  private val textViewTypeNames = Set("textview", "edittext", "button", "checkbox", "radiobutton", "textinputedittext")

  private def simpleName(call: Call): String =
    Option(call.name).getOrElse("").split("\\.").lastOption.getOrElse("").toLowerCase

  private def receiverTypes(call: Call): List[String] =
    call.receiver
      .collectAll[Expression]
      .flatMap(typeFullNameOf)
      .l

  private def typeFullNameOf(node: AstNode): Option[String] =
    node match {
      case id: Identifier => Try(id.typeFullName).toOption
      case call: Call => Try(call.typeFullName).toOption
      case local: Local => Try(local.typeFullName).toOption
      case member: Member => Try(member.typeFullName).toOption
      case param: MethodParameterIn => Try(param.typeFullName).toOption
      case typeRef: TypeRef => Try(typeRef.typeFullName).toOption
      case _ => None
    }

  def isTextViewLikeType(typeFullName: String): Boolean = {
    val value = Option(typeFullName).getOrElse("").toLowerCase
    textViewTypeNames.exists(name => value == name || value.endsWith(s".$name"))
  }

  def isTextViewLikeAnchorType(viewType: String): Boolean =
    isTextViewLikeType(viewType)

  def isListenerRegistration(call: Call): Boolean = {
    val name = simpleName(call)
    listenerNames.exists(name.contains) || name.startsWith("seton") || name.startsWith("addon")
  }

  def isGetterLike(call: Call): Boolean = {
    val name = simpleName(call)
    getterPrefixes.exists(prefix => name.startsWith(prefix))
  }

  def isReceiverMutator(call: Call, anchorViewType: String = ""): Boolean = {
    val name = simpleName(call)
    if (name == "append") {
      receiverTypes(call).exists(isTextViewLikeType) || isTextViewLikeAnchorType(anchorViewType)
    } else {
      setterPrefixes.exists(prefix => name.startsWith(prefix))
    }
  }

  def classify(call: Call): UsageKind = {
    val name = simpleName(call)
    if (ignoredNames.contains(name)) UsageKind.Other
    else if (isListenerRegistration(call)) UsageKind.Listener
    else if (isReceiverMutator(call)) UsageKind.Setter
    else if (isGetterLike(call)) UsageKind.Getter
    else UsageKind.Other
  }

  def isUiSignal(call: Call): Boolean =
    classify(call) != UsageKind.Other
}
