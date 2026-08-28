import ViewAnchorV2Contract.ResourceViewDecl

object ViewInstanceRules {
  case class ResourceRef(raw: String, owner: Option[String], name: String)
  case class BindingFieldAccess(receiver: String, field: String)

  private val rIdPattern = """(?:(\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.)?R\.id\.([A-Za-z0-9_.:-]+)""".r
  private val exactBindingFieldAccessPattern = """^\s*((?:[A-Za-z_][A-Za-z0-9_]*\.)*binding)\.([A-Za-z][A-Za-z0-9_]*)\s*$""".r
  private val holderBindingFieldPattern = """(?:^|[^A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)*binding\.([A-Za-z][A-Za-z0-9_]*)""".r
  private val receiverPattern = """^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\.|\?)""".r
  private val bindingInflatePattern = """([A-Za-z][A-Za-z0-9_]*Binding)\.(inflate|bind)""".r

  private val clickNames = Set("setOnClickListener", "setOnLongClickListener")
  private val checkedNames = Set("setOnCheckedChangeListener")
  private val sourceAffinityStopWords = Set(
    "activity",
    "binding",
    "config",
    "dialog",
    "fragment",
    "item",
    "layout",
    "screen",
    "view"
  )

  private def simpleName(callName: String): String =
    Option(callName).getOrElse("").split("\\.").lastOption.getOrElse("")

  def resourceNamesIn(code: String): List[String] =
    resourceRefsIn(code).map(_.name).distinct

  def resourceRefsIn(code: String): List[ResourceRef] =
    rIdPattern
      .findAllMatchIn(Option(code).getOrElse(""))
      .map { matched =>
        ResourceRef(
          raw = matched.matched,
          owner = Option(matched.group(1)).filter(_.nonEmpty),
          name = matched.group(2)
        )
      }
      .toList
      .distinct

  def exactBindingFieldAccess(code: String): Option[BindingFieldAccess] =
    exactBindingFieldAccessPattern
      .findFirstMatchIn(Option(code).getOrElse(""))
      .map(matched => BindingFieldAccess(receiver = matched.group(1), field = matched.group(2)))
      .filterNot(_.field == "root")

  def directBindingFieldsIn(code: String): List[String] =
    exactBindingFieldAccess(code).map(_.field).toList

  def anyBindingFieldsIn(code: String): List[String] =
    holderBindingFieldPattern
      .findAllMatchIn(Option(code).getOrElse(""))
      .map(_.group(1))
      .filterNot(_ == "root")
      .toList
      .distinct

  def bindingFieldsOnAssignmentTarget(code: String): List[String] = {
    val text = Option(code).getOrElse("")
    val target = text.split("=", 2).headOption.getOrElse("")
    anyBindingFieldsIn(target)
  }

  def receiverName(code: String): Option[String] =
    receiverPattern.findFirstMatchIn(Option(code).getOrElse("")).map(_.group(1))

  def bindingClassesIn(code: String): List[String] =
    bindingInflatePattern
      .findAllMatchIn(Option(code).getOrElse(""))
      .map(_.group(1))
      .toList
      .distinct

  def isResourceLookupName(name: String): Boolean = {
    val lower = Option(name).getOrElse("").toLowerCase
    lower.contains("findviewbyid") ||
      lower.contains("findbyid") ||
      lower.contains("requireviewbyid") ||
      lower.contains("findfragmentbyid") ||
      lower == "find"
  }

  def isResourceLookupAnchorCall(name: String, code: String): Boolean = {
    val text = Option(code).getOrElse("")
    isResourceLookupName(name) || isFallbackSingleExpressionLookup(name, text)
  }

  def isPropertyAccessAroundResourceLookup(name: String, code: String): Boolean =
    Option(name).contains("<operator>.fieldAccess") &&
      Option(code).exists(text =>
        text.contains("findViewById") ||
          text.contains("findById") ||
          text.contains("requireViewById")
      )

  def isNavigationResourceTargetCall(name: String, code: String): Boolean = {
    val lowerName = Option(name).getOrElse("").toLowerCase
    val text = Option(code).getOrElse("")
    val isFragmentCall =
      lowerName == "replace" ||
        lowerName.endsWith(".replace") ||
        lowerName == "add" ||
        lowerName.endsWith(".add") ||
        lowerName.contains("findfragmentbyid")

    isFragmentCall &&
      text.contains("R.id.") &&
      !isInteractionCall(name, text)
  }

  private def isFallbackSingleExpressionLookup(name: String, code: String): Boolean = {
    val text = Option(code).getOrElse("").trim
    val callName = Option(name).getOrElse("")
    val firstLine = text
      .split("\n")
      .find(_.trim.nonEmpty)
      .map(_.trim)
      .getOrElse("")
    val isStructuralWrapper =
      callName == "<operator>.assignment" ||
        callName == "<operator>.cast" ||
        callName == "<operator>.expressionList"

    !text.contains("\n") &&
      isStructuralWrapper &&
      !isInteractionCall(name, text) &&
      (firstLine.contains("findViewById") ||
        firstLine.contains("findById") ||
        firstLine.contains("requireViewById") ||
        firstLine.contains("findFragmentById") ||
        firstLine.contains("find(R.id."))
  }

  def isMenuItemLookupName(name: String): Boolean =
    Option(name).exists(_.equalsIgnoreCase("findItem"))

  def directInteractionKind(callName: String): Option[String] = {
    val name = simpleName(callName)
    if (clickNames.contains(name)) Some(if (name == "setOnLongClickListener") "LONG_CLICK" else "CLICK")
    else if (checkedNames.contains(name)) Some("CHECKED_CHANGE")
    else if (name == "setOnMenuItemClickListener") Some("MENU_CLICK")
    else None
  }

  def isDirectInteractionCall(callName: String): Boolean =
    directInteractionKind(callName).nonEmpty

  def interactionKind(callName: String, code: String): Option[String] = {
    directInteractionKind(callName)
      .orElse(if (Option(code).exists(_.contains(".setOnClickListener"))) Some("CLICK") else None)
      .orElse(if (Option(code).exists(_.contains(".setOnLongClickListener"))) Some("LONG_CLICK") else None)
      .orElse(if (Option(code).exists(_.contains(".setOnCheckedChangeListener"))) Some("CHECKED_CHANGE") else None)
  }

  def isInteractionCall(callName: String, code: String): Boolean =
    interactionKind(callName, code).nonEmpty

  def declViewType(decl: ResourceViewDecl): String =
    if (decl.xmlTag.nonEmpty) decl.xmlTag else "UNKNOWN"

  private def normalizeSourceHint(value: String): String =
    Option(value).getOrElse("").toLowerCase.filter(_.isLetterOrDigit)

  private def camelTokens(value: String): List[String] =
    Option(value)
      .getOrElse("")
      .stripSuffix("Binding")
      .replaceAll("([a-z0-9])([A-Z])", "$1 $2")
      .split("[^A-Za-z0-9]+")
      .toList
      .map(_.toLowerCase)
      .filter(_.nonEmpty)

  private def distinctiveBindingTokens(bindingClass: String): List[String] =
    camelTokens(bindingClass)
      .filterNot(sourceAffinityStopWords.contains)
      .filter(_.length >= 3)

  def sameSourceAffinity(filename: String, decl: ResourceViewDecl): Boolean = {
    val normalizedFile = normalizeSourceHint(filename)
    val layoutHint = decl.layoutName.exists(name => normalizedFile.contains(normalizeSourceHint(name)))
    val bindingHint = decl.bindingClass.exists { cls =>
      val normalizedStem = normalizeSourceHint(cls.stripSuffix("Binding"))
      val tokens = distinctiveBindingTokens(cls)
      val tokenHits = tokens.count(token => normalizedFile.contains(token))

      normalizedStem.nonEmpty && normalizedFile.contains(normalizedStem) ||
        tokens.lastOption.exists(token => normalizedFile.contains(token)) ||
        tokens.size >= 2 && tokenHits >= 2
    }
    layoutHint || bindingHint
  }
}
