object JavaAnchorUsagesPostProcessor {
  import JavaAnchorUsagesModel.*

  private def priority(kind: UsageKind): Int = kind match {
    case UsageKind.Listener => 5
    case UsageKind.Delegate => 4
    case UsageKind.Setter => 3
    case UsageKind.Getter => 2
    case UsageKind.Other => 1
  }

  def dedupe(usages: List[SemanticUsage]): List[SemanticUsage] =
    usages
      .filterNot(usage => isPackagePathOnly(usage.code))
      .groupBy(usage => (usage.nodeId, usage.sourceLocation.file, usage.sourceLocation.line))
      .values
      .flatMap { group =>
        group.toList.sortBy(usage => (-priority(usage.usageKind), -usage.code.length)).headOption
      }
      .toList
      .sortBy(usage => (usage.sourceLocation.file, usage.sourceLocation.line, usage.usageKind.outputLabel, usage.code))

  private def normalizedCode(usage: SemanticUsage): String =
    Option(usage.code).getOrElse("").trim

  private def dedupeContainment(usages: List[SemanticUsage]): List[SemanticUsage] = {
    val base = dedupe(usages)
    val removableIndexes = base.zipWithIndex
      .groupBy { case (usage, _) =>
        (
          usage.sourceLocation.file,
          usage.sourceLocation.line
        )
      }
      .values
      .flatMap { group =>
        val materialized = group.toList
        materialized.flatMap { case (candidate, candidateIndex) =>
          val code = normalizedCode(candidate)
          val line = candidate.sourceLocation.line
          if (code.nonEmpty && line >= 0) {
            val hasContainer = materialized.exists { case (other, otherIndex) =>
              if (otherIndex == candidateIndex) false
              else {
                val otherCode = normalizedCode(other)
                val sameKind = other.usageKind == candidate.usageKind
                val otherIsMoreSpecific = candidate.usageKind == UsageKind.Other &&
                  priority(other.usageKind) > priority(candidate.usageKind)
                otherCode.length > code.length &&
                  otherCode.contains(code) &&
                  (sameKind || otherIsMoreSpecific)
              }
            }
            if (hasContainer) Some(candidateIndex) else None
          } else None
        }
      }
      .toSet

    base.zipWithIndex
      .filterNot { case (_, index) => removableIndexes.contains(index) }
      .map(_._1)
  }

  def dedupeAcrossAnchors(reports: List[UsageReport]): List[UsageReport] =
    reports.map(report => report.copy(usages = dedupeContainment(report.usages)))
}
