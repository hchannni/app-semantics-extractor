import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Path, Paths}

object JavaOutputPathGuard {
  private val defaultRunsDir: Path =
    Paths
      .get("ai_friendly_compiler_system", "engines", "static_semantics", "scala", "runs")
      .toAbsolutePath
      .normalize

  private def rootPath(runsDir: String): Path = {
    val raw = Option(runsDir).map(_.trim).filter(_.nonEmpty)
    raw.map(Paths.get(_)).getOrElse(defaultRunsDir).toAbsolutePath.normalize
  }

  private def rejectExistingSymlinkParents(root: Path, parent: Path): Unit = {
    val relative = root.relativize(parent)
    var current = root
    relative.iterator().forEachRemaining { segment =>
      current = current.resolve(segment)
      if (Files.exists(current)) {
        require(!Files.isSymbolicLink(current), s"output parent must not contain symlink: $current")
      }
    }
  }

  def resolveOutputPath(outputPath: String, runsDir: String): Path = {
    val root = rootPath(runsDir)
    Files.createDirectories(root)
    val rootReal = root.toRealPath()
    val output = Paths.get(outputPath).toAbsolutePath.normalize
    val parent = Option(output.getParent).getOrElse(Paths.get(".").toAbsolutePath.normalize)

    require(output.startsWith(rootReal), s"outputPath must resolve under $rootReal: $output")
    rejectExistingSymlinkParents(rootReal, parent)
    Files.createDirectories(parent)
    rejectExistingSymlinkParents(rootReal, parent)
    val parentReal = parent.toRealPath()
    require(parentReal.startsWith(rootReal), s"outputPath must resolve under $rootReal: $output")
    require(!Files.isSymbolicLink(output), s"output file must not be a symlink: $output")
    output
  }

  def writeJson(outputPath: String, runsDir: String, data: String): Unit = {
    val safePath = resolveOutputPath(outputPath, runsDir)
    Files.write(safePath, data.getBytes(StandardCharsets.UTF_8))
  }
}
