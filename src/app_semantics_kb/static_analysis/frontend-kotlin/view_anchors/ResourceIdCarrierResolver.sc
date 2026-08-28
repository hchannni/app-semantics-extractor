import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

object ResourceIdCarrierResolver {
  private val resourcePattern = """[\w\.]*R(?:\d+)?\.id\.[\w_]+""".r
  private val maxAliasDepth = 4

  /** 코드 문자열에서 `R.id.*`를 추출하는 최소 단위 헬퍼다. */
  private def directResourceIdFromCode(codeOpt: Option[String]): Option[String] =
    codeOpt.flatMap(code => resourcePattern.findFirstIn(code))

  /** AST 노드에서 가장 가까운 enclosing method를 찾는다 (local alias 추적의 경계). */
  private def enclosingMethodOf(node: AstNode): Option[Method] =
    node.start
      .repeat(_.astParent)(_.emit) // CPGQL: 현재 노드에서 조상 AST를 따라 올라간다.
      .collect { case method: Method => method }
      .headOption

  /**
    * 특정 local 이름에 대한 RHS 후보를 수집한다.
    * CPGQL에서 `<operator>.assignment` call을 조회해 lhs 이름이 같은 assignment만 고른다.
    */
  private def assignmentRightHandSidesForLocal(method: Method, localName: String): List[AstNode] =
    method.call
      .nameExact("<operator>.assignment")
      .flatMap { assignment =>
        val left = assignment.argument(1).collect { case node: AstNode => node }.headOption
        val right = assignment.argument(2).collect { case node: AstNode => node }.headOption

        val isAssignmentToLocal = left.exists {
          case identifier: Identifier => Option(identifier.name).contains(localName)
          case _ => false
        }

        if (isAssignmentToLocal) right.toList else Nil
      }
      .l

  /** Identifier가 가리키는 Local 선언을 따라가 `R.id.*` 원천을 찾는다. */
  private def resourceIdFromIdentifier(
    identifier: Identifier,
    depth: Int,
    visited: Set[Long]
  )(implicit cpg: Cpg): Option[String] = {
    val localRefs =
      identifier.refsTo.collect { case local: Local => local }.l // CPGQL: IDENTIFIER -> REF -> LOCAL

    localRefs.iterator
      .flatMap(local => resourceIdFromLocal(local, depth + 1, visited + identifier.id))
      .toSeq
      .headOption
  }

  /** Local 선언의 assignment RHS를 재귀적으로 따라가 Resource ID를 복원한다. */
  private def resourceIdFromLocal(
    local: Local,
    depth: Int,
    visited: Set[Long]
  )(implicit cpg: Cpg): Option[String] = {
    if (depth > maxAliasDepth || visited.contains(local.id)) return None

    val localName = Option(local.name).getOrElse("")
    if (localName.isEmpty) return None

    enclosingMethodOf(local)
      .toList
      .flatMap { method =>
        assignmentRightHandSidesForLocal(method, localName)
          .flatMap(rhs => resourceIdFromAst(rhs, depth + 1, visited + local.id))
      }
      .headOption
  }

  /** AST 노드를 재귀적으로 해석해 direct/local alias 경로에서 `R.id.*`를 찾는다. */
  private def resourceIdFromAst(
    node: AstNode,
    depth: Int,
    visited: Set[Long]
  )(implicit cpg: Cpg): Option[String] = {
    if (depth > maxAliasDepth || visited.contains(node.id)) return None

    directResourceIdFromCode(Option(node.code)).orElse {
      node match {
        case identifier: Identifier =>
          resourceIdFromIdentifier(identifier, depth, visited + node.id)

        case call: Call =>
          // CPGQL: nested call argument까지 내려가 Resource ID carrier를 탐색한다.
          call.argument
            .collect { case arg: AstNode => arg }
            .l
            .iterator
            .flatMap(arg => resourceIdFromAst(arg, depth + 1, visited + node.id))
            .toSeq
            .headOption

        case _ =>
          None
      }
    }
  }

  /** call 인자에서 direct/local alias 기반으로 Resource ID를 복원한다. */
  def resourceIdFromCallArguments(call: Call)(implicit cpg: Cpg): Option[String] =
    call.argument
      .collect { case arg: AstNode => arg }
      .l
      .iterator
      .flatMap(arg => resourceIdFromAst(arg, depth = 0, visited = Set(call.id)))
      .toSeq
      .headOption

  /** Resource ID carrier가 하나라도 있으면 lookup candidate로 간주한다. */
  def callHasResourceIdArgument(call: Call)(implicit cpg: Cpg): Boolean =
    resourceIdFromCallArguments(call).nonEmpty
}
