object JavaViewInstanceRules {
  import JavaViewAnchorModel.ResourceViewDecl

  case class ResourceRef(raw: String, owner: Option[String], name: String)

  val DynamicResourceId = "UNKNOWN_DYNAMIC_RESOURCE"

  private val rIdPattern = """(?:(\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.)?R(?:\d+)?\.id\.([A-Za-z0-9_.:-]+)""".r
  private val resourceAliasPattern =
    """(?:^|[;\{\n]\s*)(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:final\s+)?(?:int|Integer)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;\n]+)""".r
  private val resourceArrayAliasPattern =
    """(?:^|[;\{\n]\s*)(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:final\s+)?(?:int|Integer)\s*\[\]\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:new\s+(?:int|Integer)\s*\[\]\s*)?\{([^}]*)\}""".r
  private val directBindingFieldPattern = """(?:^|[^A-Za-z0-9_])binding\.([A-Za-z][A-Za-z0-9_]*)""".r
  private val holderBindingFieldPattern = """(?:^|[^A-Za-z0-9_])(?:\w+\.)?binding\.([A-Za-z][A-Za-z0-9_]*)""".r
  private val receiverPattern = """^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\.""".r
  private val bindingInflatePattern = """([A-Za-z][A-Za-z0-9_]*Binding)\.(inflate|bind)""".r

  private val clickNames = Set("setOnClickListener", "setOnLongClickListener")
  private val checkedNames = Set("setOnCheckedChangeListener")
  private val androidViewTypePrefixes = Set(
    "android.view.",
    "android.widget.",
    "android.webkit.",
    "androidx.appcompat.widget.",
    "androidx.recyclerview.widget.",
    "com.google.android.material."
  )

  private def simpleName(callName: String): String =
    Option(callName).getOrElse("").split("\\.").lastOption.getOrElse("")

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

  def resourceAliasesIn(code: String): Map[String, List[ResourceRef]] = {
    val text = Option(code).getOrElse("")
    val scalarAliases =
      resourceAliasPattern
        .findAllMatchIn(text)
        .flatMap { matched =>
          val refs = resourceRefsIn(matched.group(2))
          if (refs.nonEmpty) Some(matched.group(1) -> refs) else None
        }
        .toList
    val arrayAliases =
      resourceArrayAliasPattern
        .findAllMatchIn(text)
        .flatMap { matched =>
          val refs = resourceRefsIn(matched.group(2))
          if (refs.nonEmpty) Some(matched.group(1) -> refs) else None
        }
        .toList

    (scalarAliases ++ arrayAliases)
      .groupMap(_._1)(_._2)
      .view
      .mapValues(_.flatten.distinct)
      .toMap
  }

  private def aliasAppears(code: String, alias: String): Boolean = {
    val pattern = s"\\b${java.util.regex.Pattern.quote(alias)}\\b".r
    pattern.findFirstIn(Option(code).getOrElse("")).nonEmpty
  }

  def resourceRefsOrAliasesIn(code: String, aliases: Map[String, List[ResourceRef]]): List[ResourceRef] = {
    val direct = resourceRefsIn(code)
    if (direct.nonEmpty) direct
    else {
      aliases
        .toList
        .filter { case (alias, _) => aliasAppears(code, alias) }
        .flatMap(_._2)
        .distinct
    }
  }

  def dynamicResourceRefsIn(callName: String, code: String, aliases: Map[String, List[ResourceRef]]): List[ResourceRef] =
    if (isResourceLookupName(callName) && resourceRefsOrAliasesIn(code, aliases).isEmpty) {
      List(ResourceRef(DynamicResourceId, None, "dynamic"))
    } else Nil

  def directBindingFieldsIn(code: String): List[String] =
    directBindingFieldPattern
      .findAllMatchIn(Option(code).getOrElse(""))
      .map(_.group(1))
      .filterNot(_ == "root")
      .toList
      .distinct

  def anyBindingFieldsIn(code: String): List[String] =
    holderBindingFieldPattern
      .findAllMatchIn(Option(code).getOrElse(""))
      .map(_.group(1))
      .filterNot(_ == "root")
      .toList
      .distinct

  def bindingFieldsOnAssignmentTarget(code: String): List[String] = {
    val target = Option(code).getOrElse("").split("=", 2).headOption.getOrElse("")
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
      lower.contains("requireviewbyid") ||
      lower.contains("findfragmentbyid") ||
      lower == "find"
  }

  def isResourceLookupAnchorCall(name: String, code: String): Boolean =
    isResourceLookupName(name) || isFallbackSingleExpressionLookup(name, code)

  def isPropertyAccessAroundResourceLookup(name: String, code: String): Boolean =
    Option(name).contains("<operator>.fieldAccess") &&
      Option(code).exists(text =>
        text.contains("findViewById") ||
          text.contains("requireViewById") ||
          text.contains("findFragmentById")
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
      text.contains("R") &&
      text.contains(".id.") &&
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
        firstLine.contains("requireViewById") ||
        firstLine.contains("findFragmentById"))
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

  def interactionKind(callName: String, code: String): Option[String] =
    directInteractionKind(callName)
      .orElse(if (Option(code).exists(_.contains(".setOnClickListener"))) Some("CLICK") else None)
      .orElse(if (Option(code).exists(_.contains(".setOnLongClickListener"))) Some("LONG_CLICK") else None)
      .orElse(if (Option(code).exists(_.contains(".setOnCheckedChangeListener"))) Some("CHECKED_CHANGE") else None)

  def isInteractionCall(callName: String, code: String): Boolean =
    interactionKind(callName, code).nonEmpty

  def isViewReceiverUsageCall(callName: String): Boolean = {
    val raw = Option(callName).getOrElse("")
    val name = simpleName(raw)
    raw.nonEmpty &&
      name.nonEmpty &&
      !raw.startsWith("<operator>.") &&
      raw != "<init>" &&
      raw != "<clinit>"
  }

  def isAndroidViewLikeType(typeFullName: String): Boolean = {
    val value = Option(typeFullName).getOrElse("")
    value == "android.view.View" ||
      androidViewTypePrefixes.exists(prefix => value.startsWith(prefix))
  }

  def declViewType(decl: ResourceViewDecl): String =
    if (decl.xmlTag.nonEmpty) decl.xmlTag else "UNKNOWN"
}
