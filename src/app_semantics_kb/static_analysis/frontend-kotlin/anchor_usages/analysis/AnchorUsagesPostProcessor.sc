object AnchorUsagesPostProcessor {
  import AnchorUsagesModel.*
  import scala.collection.mutable

  private def canonicalUsageType(anchorUsageType: String, usage: SemanticUsage): String = {
    val anchorType = Option(anchorUsageType).getOrElse("")
    if (usage.usageKind == UsageKind.Delegate) "DELEGATE"
    else if (anchorType == "RETURN" || anchorType == "SCOPE") "RETURN_SCOPE"
    else if (usage.usageKind == UsageKind.Listener || anchorType == "CHAINING") "CHAINED_CALL"
    else "ASSIGN"
  }

  private val usageTypePriority: Map[String, Int] = Map(
    "CHAINED_CALL" -> 4,
    "DELEGATE"     -> 3,
    "ASSIGN"       -> 2,
    "RETURN_SCOPE" -> 1
  ).withDefaultValue(0)

  private def normalizedCode(usage: SemanticUsage): String =
    Option(usage.code).getOrElse("").trim

  private def usageIdentityKey(usage: SemanticUsage): (String, Long, String, String) = {
    val methodFullName = Option(usage.usageMethodFullName).filter(_.nonEmpty).getOrElse(usage.methodFullName)
    (
      Option(usage.location).getOrElse(""),
      usage.nodeId,
      usage.usageKind.toString,
      methodFullName
    )
  }

  def dedupeUsages(usages: List[SemanticUsage]): List[SemanticUsage] =
    usages
      .groupBy(usageIdentityKey)
      .values
      .flatMap { group =>
        group.sortBy(u => -Option(u.code).map(_.length).getOrElse(0)).headOption
      }
      .toList

  def filterMeaningful(
    usages: List[SemanticUsage],
    isFalsePositive: String => Boolean
  ): List[SemanticUsage] =
    usages.filter { usage =>
      val code = Option(usage.code).getOrElse("").trim
      code.nonEmpty && !isFalsePositive(code)
    }

  private def dedupeSameNodeConflicts(anchor: ViewAnchor, usages: List[SemanticUsage]): List[SemanticUsage] = {
    val (withNodeId, noNodeId) = usages.partition(_.nodeId > 0)

    val dedupedByNode = withNodeId
      .groupBy(_.nodeId)
      .values
      .flatMap { group =>
        group.toList
          .sortBy { usage =>
            val usageType = canonicalUsageType(anchor.usageType, usage)
            (-usageTypePriority(usageType), -normalizedCode(usage).length)
          }
          .headOption
      }
      .toList

    dedupedByNode ++ noNodeId
  }

  private def dropPackagePathNoise(usages: List[SemanticUsage]): List[SemanticUsage] =
    usages.filterNot(usage => isPackagePathOnly(normalizedCode(usage)))

  def dedupeForOutput(anchor: ViewAnchor, usages: List[SemanticUsage]): List[SemanticUsage] = {
    val nodeDeduped = dedupeSameNodeConflicts(anchor, usages)
    dropPackagePathNoise(nodeDeduped)
  }

  // resourceId는 TaggedUsage에서 직접 관리 (SemanticUsage에서 제거됨)
  private case class TaggedUsage(reportIndex: Int, localIndex: Int, resourceId: String, usage: SemanticUsage)

  private def semanticKindPriority(usage: SemanticUsage): Int = usage.usageKind match {
    case UsageKind.Listener => 4
    case UsageKind.Delegate => 3
    case UsageKind.Setter   => 2
    case UsageKind.Getter   => 1
    case UsageKind.Other    => 0
  }

  private def preferredTagged(group: List[TaggedUsage]): Option[TaggedUsage] =
    group.sortBy { tagged =>
      (
        -semanticKindPriority(tagged.usage),
        -normalizedCode(tagged.usage).length,
        tagged.reportIndex,
        tagged.localIndex
      )
    }.headOption

  def dedupeAcrossAnchors(reports: List[UsageReport]): List[UsageReport] = {
    val tagged = reports.zipWithIndex.flatMap { case (report, reportIndex) =>
      report.usages.zipWithIndex.map { case (usage, localIndex) =>
        TaggedUsage(reportIndex = reportIndex, localIndex = localIndex, resourceId = report.anchor.resourceId, usage = usage)
      }
    }

    val pathFiltered = tagged.filterNot(taggedUsage => isPackagePathOnly(normalizedCode(taggedUsage.usage)))

    val nodeDeduped = pathFiltered
      .groupBy(taggedUsage => (taggedUsage.resourceId, taggedUsage.usage.nodeId))
      .values
      .flatMap { group =>
        if (group.headOption.exists(_.usage.nodeId > 0)) preferredTagged(group.toList)
        else group
      }
      .toList

    val exactDeduped = nodeDeduped
      .groupBy { taggedUsage =>
        (
          taggedUsage.resourceId,
          taggedUsage.usage.sourceLocation.file,
          taggedUsage.usage.sourceLocation.line,
          normalizedCode(taggedUsage.usage)
        )
      }
      .values
      .flatMap(group => preferredTagged(group.toList))
      .toList

    val removableIds = mutable.HashSet.empty[(Int, Int)]
    exactDeduped
      .groupBy(taggedUsage =>
        (
          taggedUsage.resourceId,
          taggedUsage.usage.sourceLocation.file,
          taggedUsage.usage.sourceLocation.line
        )
      )
      .values
      .foreach { group =>
        val materialized = group.toList
        materialized.foreach { candidate =>
          val code = normalizedCode(candidate.usage)
          val line = candidate.usage.sourceLocation.line
          if (code.nonEmpty && line >= 0) {
            val hasContainer = materialized.exists { other =>
              if (other.reportIndex == candidate.reportIndex && other.localIndex == candidate.localIndex) false
              else {
                val otherCode = normalizedCode(other.usage)
                otherCode.length > code.length && otherCode.contains(code)
              }
            }
            if (hasContainer) {
              removableIds += ((candidate.reportIndex, candidate.localIndex))
            }
          }
        }
      }

    val containmentDeduped = exactDeduped.filterNot(taggedUsage =>
      removableIds.contains((taggedUsage.reportIndex, taggedUsage.localIndex))
    )

    val keepByReport = containmentDeduped
      .groupBy(_.reportIndex)
      .view
      .mapValues(_.sortBy(_.localIndex).map(_.usage))
      .toMap

    reports.zipWithIndex.map { case (report, reportIndex) =>
      report.copy(usages = keepByReport.getOrElse(reportIndex, Nil))
    }
  }
}
