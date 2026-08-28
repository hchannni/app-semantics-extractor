import ViewAnchorV2Contract.ViewInstance

object ViewInstanceCanonicalizer {
  private case class CanonicalKey(
    resourceId: String,
    handleIdentity: String,
    location: String
  )

  private def handleIdentity(instance: ViewInstance): String =
    instance.handleOwnerNodeId
      .map(id => s"node:$id")
      .orElse {
        instance.handleName.map(name => s"name:${instance.bindingClass.getOrElse("")}:$name")
      }
      .getOrElse(s"inline:${instance.cpgNodeId}")

  private def canonicalKey(instance: ViewInstance): CanonicalKey =
    CanonicalKey(
      resourceId = instance.resourceId,
      handleIdentity = handleIdentity(instance),
      location = if (instance.handleName.nonEmpty) "" else instance.location
    )

  private def isHandle(instance: ViewInstance): Boolean =
    instance.occurrenceRole == "HANDLE"

  private def roleRank(instance: ViewInstance): Int =
    if (isHandle(instance)) 0 else 1

  private def expressionRank(instance: ViewInstance): Int = {
    val code = Option(instance.code).getOrElse("")
    if (code.contains("findViewById") || code.contains("binding.")) 0
    else 1
  }

  private def ownerExpressionRank(instance: ViewInstance): Int = {
    val code = Option(instance.code).getOrElse("").trim
    val handleName = instance.handleName.getOrElse("")
    val bindingField = instance.bindingField.getOrElse("")

    if (handleName.nonEmpty &&
        (code.startsWith(s"val $handleName") ||
          code.startsWith(s"var $handleName") ||
          code.startsWith(s"$handleName =") ||
          code.startsWith(s"$handleName by "))) 0
    else if (bindingField.nonEmpty && code == s"binding.$bindingField") 0
    else if (handleName.nonEmpty && code.contains(handleName)) 1
    else 2
  }

  private def representative(instances: List[ViewInstance]): ViewInstance =
    instances
      .sortBy { instance =>
        (
          roleRank(instance),
          ownerExpressionRank(instance),
          expressionRank(instance),
          instance.code.length,
          instance.cpgNodeId
        )
      }
      .head

  private def firstMeaningfulLine(code: String): String =
    Option(code).getOrElse("")
      .split("\n")
      .find(_.trim.nonEmpty)
      .map(_.trim)
      .getOrElse("")

  private def resourceName(instance: ViewInstance): String =
    instance.resourceId.split("\\.").lastOption.getOrElse(instance.resourceId)

  private def isNestedContainerExpression(instance: ViewInstance): Boolean = {
    val code = Option(instance.code).getOrElse("")
    if (!code.contains("\n")) return false

    val firstLine = firstMeaningfulLine(code)
    val name = resourceName(instance)
    val bindingField = instance.bindingField.getOrElse("")
    !firstLine.contains(name) && (bindingField.isEmpty || !firstLine.contains(bindingField))
  }

  def canonicalize(instances: List[ViewInstance]): List[ViewInstance] =
    instances
      .filterNot(isNestedContainerExpression)
      .groupBy(canonicalKey)
      .values
      .map(group => representative(group.toList))
      .toList
      .sortBy(instance => (instance.location, instance.resourceId, instance.cpgNodeId))

  def canonicalizeHandles(instances: List[ViewInstance]): List[ViewInstance] =
    canonicalize(instances.filter(isHandle))
}
