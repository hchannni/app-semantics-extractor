object JavaViewInstanceCanonicalizer {
  import JavaViewAnchorModel.JavaViewInstance

  private case class CanonicalKey(
    resourceId: String,
    handleIdentity: String,
    location: String
  )

  private def handleIdentity(instance: JavaViewInstance): String =
    instance.handleOwnerNodeId
      .map(id => s"node:$id")
      .orElse {
        instance.handleName.map(name => s"name:${instance.bindingClass.getOrElse("")}:$name")
      }
      .getOrElse(s"inline:${instance.cpgNodeId}")

  private def canonicalKey(instance: JavaViewInstance): CanonicalKey =
    CanonicalKey(
      resourceId = instance.resourceId,
      handleIdentity = handleIdentity(instance),
      location = if (instance.handleName.nonEmpty) "" else instance.location
    )

  private def isHandle(instance: JavaViewInstance): Boolean =
    instance.occurrenceRole == "HANDLE"

  private def roleRank(instance: JavaViewInstance): Int =
    if (isHandle(instance)) 0 else 1

  private def expressionRank(instance: JavaViewInstance): Int = {
    val code = Option(instance.code).getOrElse("")
    if (code.contains("findViewById") || code.contains("requireViewById") || code.contains("binding.")) 0
    else 1
  }

  private def ownerExpressionRank(instance: JavaViewInstance): Int = {
    val code = Option(instance.code).getOrElse("").trim
    val handleName = instance.handleName.getOrElse("")
    val bindingField = instance.bindingField.getOrElse("")

    if (handleName.nonEmpty &&
        (code.startsWith(s"$handleName =") ||
          code.startsWith(s"this.$handleName =") ||
          code.contains(s" $handleName ="))) 0
    else if (bindingField.nonEmpty && code == s"binding.$bindingField") 0
    else if (handleName.nonEmpty && code.contains(handleName)) 1
    else 2
  }

  private def representative(instances: List[JavaViewInstance]): JavaViewInstance =
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

  private def resourceName(instance: JavaViewInstance): String =
    instance.resourceId.split("\\.").lastOption.getOrElse(instance.resourceId)

  private def isNestedContainerExpression(instance: JavaViewInstance): Boolean = {
    val code = Option(instance.code).getOrElse("")
    if (!code.contains("\n")) return false

    val firstLine = firstMeaningfulLine(code)
    val name = resourceName(instance)
    val bindingField = instance.bindingField.getOrElse("")
    !firstLine.contains(name) && (bindingField.isEmpty || !firstLine.contains(bindingField))
  }

  def canonicalize(instances: List[JavaViewInstance]): List[JavaViewInstance] =
    instances
      .filterNot(isNestedContainerExpression)
      .groupBy(canonicalKey)
      .values
      .map(group => representative(group.toList))
      .toList
      .sortBy(instance => (instance.location, instance.resourceId, instance.cpgNodeId))

  def canonicalizeHandles(instances: List[JavaViewInstance]): List[JavaViewInstance] =
    canonicalize(instances.filter(isHandle))
}
