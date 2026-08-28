import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.collection.mutable
import scala.util.Try

import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Paths}

object MethodCfgAnalysis {
  // -------------------------------------------------
  // Configuration constants
  // -------------------------------------------------

  /**
   * CFG 분석 시 사용되는 제한 값들
   */
  private object CfgAnalysisLimits {
    val MAX_NODES_PER_BLOCK        = 400
    val MAX_INSTRUCTIONS_PER_BLOCK = 80
    val MAX_BLOCK_PATHS            = 8
    val MAX_BLOCK_PATH_DEPTH       = 20
    val MIN_LIMIT                  = 1
  }

  private val frameworkPrefixes = List(
    "android.",
    "androidx.",
    "com.google.android.material."
  )
  private val frameworkTypesCache = mutable.Map.empty[Long, List[String]]

  def loadCpg(path: String): Cpg =
    io.joern.joerncli.console.Joern
      .importCpg(path)
      .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

  private def methodLocation(m: Method): String =
    s"${Option(m.filename).getOrElse("?")}:${m.lineNumber.getOrElse(-1)}"

  private def frameworkTypesOf(method: Method)(implicit cpg: Cpg): List[String] =
    frameworkTypesCache.getOrElseUpdate(
      method.id, {
        val found = method.definingTypeDecl.headOption.toList.flatMap { td =>
          val direct   = Option(td.fullName).toList
          val inherited = td.inheritsFromTypeFullName.l.flatMap(Option(_))
          val bases    = td.baseTypeDecl.fullName.l.flatMap(Option(_))
          (direct ++ inherited ++ bases).distinct
        }
        found.filter(t => frameworkPrefixes.exists(prefix => t.startsWith(prefix))).distinct
      }
    )

  // -------------------------------------------------
  // Main logic - Callback method resolution
  // -------------------------------------------------

  /**
   * 주어진 콜백 메서드 ID, fullName, name을 기반으로 실제 메서드 노드를 찾는 함수
   */
  def resolveTargetMethod(
    methodId: Long,
    methodFullName: String,
    methodName: String
  )(implicit cpg: Cpg): Method = {
    val selectorsUsed =
      List(
        if (methodId > 0) Some("id") else None,
        if (methodFullName.nonEmpty) Some("fullName") else None,
        if (methodName.nonEmpty) Some("name") else None
      ).flatten

    if (selectorsUsed.size > 1) {
      throw new RuntimeException(
        "Provide exactly one selector: methodId OR methodFullName OR methodName"
      )
    }
    if (selectorsUsed.isEmpty) {
      throw new RuntimeException(
        "Provide one selector: methodId OR methodFullName OR methodName"
      )
    }

    val candidates: List[Method] =
      selectorsUsed.head match {
        case "id" =>
          Try(cpg.graph.node(methodId)).toOption.collect { case m: Method => m }.toList
        case "fullName" =>
          cpg.method.fullNameExact(methodFullName).l
        case "name" =>
          cpg.method.nameExact(methodName).l
      }

    val distinctCandidates = candidates.distinctBy(_.id)
    if (distinctCandidates.isEmpty) {
      throw new RuntimeException("No method matched the provided selector.")
    }
    if (distinctCandidates.size > 1) {
      val listed = distinctCandidates
        .map(m => s"  - id=${m.id}, fullName=${m.fullName}, loc=${methodLocation(m)}")
        .mkString("\n")
      throw new RuntimeException(s"Selector matched multiple methods. Narrow it down.\n$listed")
    }
    distinctCandidates.head
  }

  // -------------------------------------------------
  // Helper functions
  // -------------------------------------------------

  private def escape(s: String): String =
    Option(s).getOrElse("").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

  private def asString(value: Any): String =
    Option(value).map(_.toString).getOrElse("")

  private def nodeTypeOf(node: CfgNode): String = {
    val fromTyped = node match {
      case i: Identifier        => Option(i.typeFullName).getOrElse("")
      case c: Call              => Option(c.typeFullName).getOrElse("")
      case l: Local             => Option(l.typeFullName).getOrElse("")
      case p: MethodParameterIn => Option(p.typeFullName).getOrElse("")
      case m: Member            => Option(m.typeFullName).getOrElse("")
      case lit: Literal         => Option(lit.typeFullName).getOrElse("")
      case t: TypeRef           => Option(t.typeFullName).getOrElse("")
      case _                    => ""
    }

    if (fromTyped.nonEmpty) fromTyped
    else {
      Try(node.propertyOption("TYPE_FULL_NAME").orNull).toOption
        .map(asString)
        .getOrElse("")
    }
  }

  def writeUtf8(path: String, text: String): Unit = {
    val p = Paths.get(path)
    Option(p.getParent).foreach(parent => Files.createDirectories(parent))
    Files.write(p, text.getBytes(StandardCharsets.UTF_8))
  }

  private def nodeOrderingKey(node: CfgNode): (Int, Int, Long) =
    (node.lineNumber.getOrElse(Int.MaxValue), node.order, node.id)

  private def hasRealCode(node: CfgNode): Boolean = {
    val code = Option(node.code).getOrElse("").trim
    code.nonEmpty && code != "<empty>" && code != "<unknown>"
  }

  // -------------------------------------------------
  // Instruction extraction helpers
  // -------------------------------------------------

  /**
   * Statement-level node: AST parent이 Block 또는 Method인 노드.
   * 이 조건을 만족하는 노드는 소스 코드에서 단독 statement에 해당하며,
   * sub-expression 중복 없이 명확한 기준으로 instruction을 추출할 수 있다.
   */
  private def isStatementLevelNode(node: CfgNode): Boolean =
    Try {
      node.astParent match {
        case _: Block | _: Method => true
        case _                    => false
      }
    }.getOrElse(false)

  private val statementOperatorFullNames: Set[String] = Set(
    "<operator>.assignment",
    "<operator>.assignmentPlus",
    "<operator>.assignmentMinus",
    "<operator>.assignmentMultiplication",
    "<operator>.assignmentDivision",
    "<operator>.assignmentModulo",
    "<operator>.assignmentAnd",
    "<operator>.assignmentOr",
    "<operator>.assignmentXor",
    "<operator>.assignmentShiftLeft",
    "<operator>.assignmentArithmeticShiftRight",
    "<operator>.assignmentLogicalShiftRight"
  )

  private def isStatementLikeCall(call: Call): Boolean = {
    val fullName = Option(call.methodFullName).getOrElse("")
    val name     = Option(call.name).getOrElse("")
    val isOperator = fullName.startsWith("<operator>.") || name.startsWith("<operator>.")
    !isOperator || statementOperatorFullNames.contains(fullName)
  }

  private def isPrimaryInstructionNode(node: CfgNode): Boolean = {
    Option(node.label).getOrElse("") match {
      case "CONTROL_STRUCTURE" | "RETURN" =>
        hasRealCode(node)
      case "CALL" =>
        node match {
          case c: Call => hasRealCode(c) && isStatementLikeCall(c)
          case _       => false
        }
      case _ => false
    }
  }

  private def isFallbackInstructionNodeStrict(node: CfgNode): Boolean = {
    val label = Option(node.label).getOrElse("")
    label match {
      case "CALL" | "CONTROL_STRUCTURE" | "RETURN" => hasRealCode(node)
      case _                                        => false
    }
  }

  private def isFallbackInstructionNodeBroad(node: CfgNode): Boolean = {
    val label = Option(node.label).getOrElse("")
    label match {
      case "CALL" | "CONTROL_STRUCTURE" | "RETURN" | "IDENTIFIER" | "LITERAL" |
           "TYPE_REF" | "FIELD_IDENTIFIER" | "METHOD_REF" =>
        hasRealCode(node)
      case _ => false
    }
  }

  /**
   * 블록 내 instruction 목록을 추출.
   * 1순위: statement-level 노드 (AST parent = Block/Method) → sub-expression 중복 없음
   * 2순위: primary (CALL/RETURN/CONTROL_STRUCTURE 중 statement-like)
   * 3순위: fallback (strict → broad)
   */
  private def extractInstructions(
    blockNodes: List[CfgNode],
    lineStart: Int
  ): (String, List[String]) = {
    val stmtLevelInstructions = blockNodes
      .filter(isStatementLevelNode)
      .map(n => Option(n.code).getOrElse("").trim)
      .filter(_.nonEmpty)

    if (stmtLevelInstructions.nonEmpty) {
      return ("statement", stmtLevelInstructions)
    }

    val primaryInstructions = blockNodes
      .filter(isPrimaryInstructionNode)
      .map(n => Option(n.code).getOrElse("").trim)
      .filter(_.nonEmpty)

    if (primaryInstructions.nonEmpty) {
      return ("primary", primaryInstructions)
    }

    val strictFallback = blockNodes
      .filter(isFallbackInstructionNodeStrict)
      .map(n => Option(n.code).getOrElse("").trim)
      .filter(_.nonEmpty)

    val fallback =
      if (strictFallback.nonEmpty) strictFallback
      else
        blockNodes
          .filter(isFallbackInstructionNodeBroad)
          .map(n => Option(n.code).getOrElse("").trim)
          .filter(_.nonEmpty)

    val ensured =
      if (fallback.nonEmpty) fallback
      else List(s"<no-instruction line=${lineStart}>")

    ("fallback", ensured)
  }

  // -------------------------------------------------
  // Branch detection helpers
  // -------------------------------------------------

  /**
   * 분기 노드에서 AST를 위로 걸어올라 가장 가까운 CONTROL_STRUCTURE의 type을 찾는다.
   * Kotlin CPG에서 분기 노드는 CALL(조건식)이고 CONTROL_STRUCTURE는 그 AST 부모인 경우가 많다.
   */
  private def findEnclosingCsType(node: CfgNode, maxDepth: Int = 6): Option[String] =
    if (maxDepth <= 0) None
    else
      Try {
        node.astParent match {
          case cs: ControlStructure => Option(cs.controlStructureType).filter(_.nonEmpty)
          case parent: CfgNode      => findEnclosingCsType(parent, maxDepth - 1)
          case _                    => None
        }
      }.getOrElse(None)

  /**
   * 블록의 분기 노드에서 control structure type과 predicate를 추출.
   * Returns (branchType: String, predicate: Option[String])
   */
  private def detectBranchInfo(
    blockNodes: List[CfgNode],
    succMap: Map[Long, List[Long]],
    connectedNodeIdSet: Set[Long]
  ): (String, Option[String]) = {
    val branchNodeOpt = blockNodes.find { n =>
      succMap.getOrElse(n.id, Nil).count(connectedNodeIdSet.contains) > 1
    }
    branchNodeOpt match {
      case None => ("FLOW", None)
      case Some(cs: ControlStructure) =>
        val csType    = Option(cs.controlStructureType).filter(_.nonEmpty).getOrElse("IF")
        val predicate = Option(cs.code).map(_.trim).filter(s => s.nonEmpty && s != csType)
        (csType, predicate)
      case Some(branchNode) =>
        val predicate = Option(branchNode.code).map(_.trim).filter(_.nonEmpty)
        // Walk up AST to find enclosing CONTROL_STRUCTURE
        val csType = findEnclosingCsType(branchNode).getOrElse {
          branchNode match {
            case call: Call
                if Option(call.methodFullName).exists(fn =>
                  fn == "<operator>.conditional" || fn.endsWith(".conditional")) =>
              "CONDITIONAL"
            case _ =>
              // Most Kotlin/Java conditions without an enclosing CS are IF-based
              "IF"
          }
        }
        (csType, predicate)
    }
  }

  /**
   * 분기 타입에 따라 후속 블록 인덱스에 edge label을 할당.
   * Target block의 첫 번째 노드 line number로 정렬하여 순서 결정.
   */
  private def computeBranchEdgeLabels(
    branchType: String,
    succBlockIdxs: List[Int],
    blockNodeLists: List[List[Long]],
    connectedNodesById: Map[Long, CfgNode]
  ): Map[Int, String] = {
    if (succBlockIdxs.size <= 1) return Map.empty

    def firstLineOf(blockIdx: Int): Int =
      blockNodeLists(blockIdx)
        .flatMap(id => connectedNodesById.get(id).flatMap(_.lineNumber))
        .minOption
        .getOrElse(Int.MaxValue)

    val sortedByLine = succBlockIdxs.sortBy(firstLineOf)

    branchType match {
      case "IF" | "CONDITIONAL" =>
        sortedByLine.zipWithIndex.map { case (idx, i) =>
          idx -> (if (i == 0) "true" else "false")
        }.toMap
      case "WHILE" | "FOR" | "DO" =>
        sortedByLine.zipWithIndex.map { case (idx, i) =>
          idx -> (if (i == 0) "loop_body" else "loop_exit")
        }.toMap
      case "TRY" =>
        Map(sortedByLine.head -> "normal") ++ sortedByLine.tail.map(_ -> "exception")
      case "SWITCH" =>
        sortedByLine.zipWithIndex.map { case (idx, i) => idx -> s"case_$i" }.toMap
      case _ =>
        sortedByLine.zipWithIndex.map { case (idx, i) => idx -> s"branch_$i" }.toMap
    }
  }

  // -------------------------------------------------
  // CFG semantic flag helpers
  // -------------------------------------------------

  private def detectHasLoop(connectedNodesById: Map[Long, CfgNode]): Boolean =
    connectedNodesById.values.exists {
      case cs: ControlStructure =>
        val t = Option(cs.controlStructureType).getOrElse("")
        t == "WHILE" || t == "FOR" || t == "DO"
      case _ => false
    }

  private def detectHasTryCatch(connectedNodesById: Map[Long, CfgNode]): Boolean =
    connectedNodesById.values.exists {
      case cs: ControlStructure =>
        Option(cs.controlStructureType).exists(_ == "TRY")
      case _ => false
    }

  // -------------------------------------------------
  // Main logic - CFG node classification
  // -------------------------------------------------

  private case class CfgMaps(
    cfgNodes: List[CfgNode],
    nodeIdSet: Set[Long],
    nodesById: Map[Long, CfgNode],
    succMap: Map[Long, List[Long]],
    predMap: Map[Long, List[Long]],
    entryNodeIds: List[Long],
    exitNodeIds: List[Long],
    branchNodeIds: List[Long],
    joinNodeIds: List[Long],
    edgeCount: Int,
    cfgLastNodeIds: List[Long],
    methodCfgNextNodeIds: List[Long],
    connectedNodeIdSet: Set[Long],
    connectedNodesById: Map[Long, CfgNode]
  ) {
    def sortedNodeIds(ids: Iterable[Long]): List[Long] =
      ids
        .toList
        .distinct
        .flatMap(id => connectedNodesById.get(id).map(n => id -> n))
        .sortBy { case (_, n) => nodeOrderingKey(n) }
        .map(_._1)
  }

  private def buildCfgMaps(method: Method): CfgMaps = {
    val cfgNodes  = method.cfgNode.collect { case n: CfgNode => n }.l.distinctBy(_.id)
    val nodeIdSet = cfgNodes.map(_.id).toSet
    val nodesById = cfgNodes.map(n => n.id -> n).toMap

    def inMethod(nodes: List[CfgNode]): List[CfgNode] =
      nodes.filter(n => nodeIdSet.contains(n.id)).distinctBy(_.id)

    val succMap: Map[Long, List[Long]] = cfgNodes.map { node =>
      val succ = inMethod(node.cfgNext.collect { case n: CfgNode => n }.l).map(_.id).distinct
      node.id -> succ
    }.toMap

    val predMap: Map[Long, List[Long]] = cfgNodes.map { node =>
      val pred = inMethod(node.cfgPrev.collect { case n: CfgNode => n }.l).map(_.id).distinct
      node.id -> pred
    }.toMap

    val entryNodeIds  = cfgNodes.filter(n => predMap.getOrElse(n.id, Nil).isEmpty).map(_.id).sorted
    val exitNodeIds   = cfgNodes.filter(n => succMap.getOrElse(n.id, Nil).isEmpty).map(_.id).sorted
    val branchNodeIds = cfgNodes.filter(n => succMap.getOrElse(n.id, Nil).size > 1).map(_.id).sorted
    val joinNodeIds   = cfgNodes.filter(n => predMap.getOrElse(n.id, Nil).size > 1).map(_.id).sorted

    val edgeCount             = succMap.values.map(_.size).sum
    val cfgLastNodeIds        = method.start.cfgLast.collect { case n: CfgNode => n.id }.l.distinct.sorted
    val methodCfgNextNodeIds  = method.start.cfgNext.collect { case n: CfgNode => n.id }.l.distinct.sorted

    val connectedNodeIdSetRaw = cfgNodes
      .filter(node => predMap.getOrElse(node.id, Nil).nonEmpty || succMap.getOrElse(node.id, Nil).nonEmpty)
      .map(_.id)
      .toSet
    val connectedNodeIdSet =
      if (connectedNodeIdSetRaw.nonEmpty) connectedNodeIdSetRaw else nodeIdSet
    val connectedNodesById = nodesById.filter { case (id, _) => connectedNodeIdSet.contains(id) }

    CfgMaps(
      cfgNodes, nodeIdSet, nodesById,
      succMap, predMap,
      entryNodeIds, exitNodeIds, branchNodeIds, joinNodeIds,
      edgeCount, cfgLastNodeIds, methodCfgNextNodeIds,
      connectedNodeIdSet, connectedNodesById
    )
  }

  // -------------------------------------------------
  // Main logic - CFG block analysis (Step 2: Identify Basic Blocks)
  // -------------------------------------------------

  private case class BasicBlockStructure(
    blockNodeLists: List[List[Long]],
    nodeToBlockIdx: Map[Long, Int],
    sortedBlockEdgesIdx: List[(Int, Int)],
    succBlockMap: Map[Int, Set[Int]],
    predBlockMap: Map[Int, Set[Int]],
    preferredEntryNodeIds: List[Long]
  ) {
    def blockName(idx: Int): String = s"B${idx + 1}"

    def entryBlockIdxSet: Set[Int] =
      preferredEntryNodeIds.flatMap(nodeToBlockIdx.get).toSet

    def exitBlockIdxSet: Set[Int] =
      blockNodeLists.indices.filter(idx => succBlockMap(idx).isEmpty).toSet

    def branchBlockIdxSet: Set[Int] =
      blockNodeLists.indices.filter(idx => succBlockMap(idx).size > 1).toSet

    def joinBlockIdxSet: Set[Int] =
      blockNodeLists.indices.filter(idx => predBlockMap(idx).size > 1).toSet
  }

  private def identifyBasicBlocks(cfgMaps: CfgMaps): BasicBlockStructure = {
    val succMap             = cfgMaps.succMap
    val predMap             = cfgMaps.predMap
    val entryNodeIds        = cfgMaps.entryNodeIds
    val methodCfgNextNodeIds = cfgMaps.methodCfgNextNodeIds
    val connectedNodeIdSet  = cfgMaps.connectedNodeIdSet

    val preferredEntryNodeIds =
      if (methodCfgNextNodeIds.nonEmpty) methodCfgNextNodeIds.filter(connectedNodeIdSet.contains)
      else entryNodeIds.filter(connectedNodeIdSet.contains)

    val leaderIds = mutable.LinkedHashSet.empty[Long]
    cfgMaps.sortedNodeIds(preferredEntryNodeIds).foreach(leaderIds += _)

    cfgMaps.connectedNodesById.values.toList.sortBy(nodeOrderingKey).foreach { node =>
      val preds = predMap.getOrElse(node.id, Nil).filter(connectedNodeIdSet.contains)
      val isLeader =
        preds.isEmpty ||
          preds.size != 1 ||
          preds.exists(predId => succMap.getOrElse(predId, Nil).count(connectedNodeIdSet.contains) != 1)
      if (isLeader) leaderIds += node.id
    }

    val visitedNodeIds  = mutable.Set.empty[Long]
    val blockNodeLists  = mutable.ArrayBuffer.empty[List[Long]]
    val nodeToBlockIdx  = mutable.Map.empty[Long, Int]

    def appendBlockFrom(startId: Long): Unit = {
      if (visitedNodeIds.contains(startId)) return

      val blockNodes = mutable.ListBuffer.empty[Long]
      var current    = startId
      var continue   = true

      while (continue && !visitedNodeIds.contains(current)) {
        blockNodes += current
        visitedNodeIds += current

        val succ = succMap.getOrElse(current, Nil).filter(connectedNodeIdSet.contains)
        if (succ.size == 1) {
          val next     = succ.head
          val nextPred = predMap.getOrElse(next, Nil).filter(connectedNodeIdSet.contains)
          val canContinue =
            !visitedNodeIds.contains(next) &&
              nextPred.size == 1 &&
              !leaderIds.contains(next)
          if (canContinue) current = next
          else continue = false
        } else {
          continue = false
        }
      }

      val idx       = blockNodeLists.size
      val blockList = blockNodes.toList
      blockNodeLists += blockList
      blockList.foreach(id => nodeToBlockIdx.put(id, idx))
    }

    cfgMaps.sortedNodeIds(leaderIds).foreach(appendBlockFrom)
    cfgMaps.sortedNodeIds(connectedNodeIdSet).foreach { id =>
      if (!visitedNodeIds.contains(id)) appendBlockFrom(id)
    }

    val blockEdgesIdx = mutable.LinkedHashSet.empty[(Int, Int)]
    blockNodeLists.zipWithIndex.foreach { case (nodeIds, fromIdx) =>
      nodeIds.foreach { nodeId =>
        succMap.getOrElse(nodeId, Nil).filter(connectedNodeIdSet.contains).foreach { succId =>
          nodeToBlockIdx.get(succId).foreach { toIdx =>
            if (fromIdx != toIdx) blockEdgesIdx += ((fromIdx, toIdx))
          }
        }
      }
    }
    val sortedBlockEdgesIdx = blockEdgesIdx.toList.sortBy { case (from, to) => (from, to) }

    val tempSuccBlockMap = blockNodeLists.indices.map(idx => idx -> mutable.LinkedHashSet.empty[Int]).toMap
    val tempPredBlockMap = blockNodeLists.indices.map(idx => idx -> mutable.LinkedHashSet.empty[Int]).toMap
    sortedBlockEdgesIdx.foreach { case (from, to) =>
      tempSuccBlockMap(from) += to
      tempPredBlockMap(to) += from
    }

    BasicBlockStructure(
      blockNodeLists.toList,
      nodeToBlockIdx.toMap,
      sortedBlockEdgesIdx,
      tempSuccBlockMap.view.mapValues(_.toSet).toMap,
      tempPredBlockMap.view.mapValues(_.toSet).toMap,
      preferredEntryNodeIds
    )
  }

  // -------------------------------------------------
  // Main logic - CFG block analysis (Step 3: Extract Block Metadata)
  // -------------------------------------------------

  /**
   * 블록 메타데이터 추출 결과 (통합 cfg 객체 + edge label 맵)
   */
  private case class BlockMetadataResult(
    cfgBlocks: List[ujson.Obj],
    edgeLabels: Map[(Int, Int), (String, Option[String])]
  )

  /**
   * 각 블록의 통합 JSON 객체와 edge label 맵을 생성.
   * basicBlockCfg + traditionalCfg 통합 → 단일 cfg.blocks[] 형식.
   */
  private def extractBlockMetadata(
    cfgMaps: CfgMaps,
    blockStructure: BasicBlockStructure,
    maxNodesPerBlock: Int,
    maxInstructionsPerBlock: Int,
    includeBlockNodeDetails: Boolean
  ): BlockMetadataResult = {
    val succMap            = cfgMaps.succMap
    val connectedNodeIdSet = cfgMaps.connectedNodeIdSet
    val connectedNodesById = cfgMaps.connectedNodesById
    val blockNodeLists     = blockStructure.blockNodeLists
    val nodeToBlockIdx     = blockStructure.nodeToBlockIdx
    val succBlockMap       = blockStructure.succBlockMap
    val predBlockMap       = blockStructure.predBlockMap

    val effectiveMaxNodes = math.min(
      math.max(maxNodesPerBlock, CfgAnalysisLimits.MIN_LIMIT),
      CfgAnalysisLimits.MAX_NODES_PER_BLOCK
    )
    val effectiveMaxInstrs = math.min(
      math.max(maxInstructionsPerBlock, CfgAnalysisLimits.MIN_LIMIT),
      CfgAnalysisLimits.MAX_INSTRUCTIONS_PER_BLOCK
    )

    val entryBlockIdxSet  = blockStructure.entryBlockIdxSet
    val exitBlockIdxSet   = blockStructure.exitBlockIdxSet
    val branchBlockIdxSet = blockStructure.branchBlockIdxSet
    val joinBlockIdxSet   = blockStructure.joinBlockIdxSet

    val edgeLabels = mutable.Map.empty[(Int, Int), (String, Option[String])]

    val cfgBlocks = blockNodeLists.zipWithIndex.map { case (nodeIds, idx) =>
      val blockNodes  = nodeIds.flatMap(connectedNodesById.get).sortBy(nodeOrderingKey)
      val lines       = blockNodes.map(_.lineNumber.getOrElse(-1)).filter(_ >= 0)
      val lineStart   = if (lines.nonEmpty) lines.min else -1
      val lineEnd     = if (lines.nonEmpty) lines.max else -1
      val limitedNodes = blockNodes.take(effectiveMaxNodes)

      val types       = limitedNodes.map(nodeTypeOf).filter(_.nonEmpty).distinct.sorted
      val identifiers = limitedNodes.collect { case i: Identifier => i.name }.filter(_.nonEmpty).distinct.sorted

      val (_, rawInstructions) = extractInstructions(blockNodes, lineStart)
      val instructions = rawInstructions.take(effectiveMaxInstrs)

      val predBlocks = predBlockMap(idx).toList.sorted.map(blockStructure.blockName)
      val succBlocks = succBlockMap(idx).toList.sorted.map(blockStructure.blockName)

      // Terminator: detect branch type and compute edge labels for this block
      val terminatorKind = if (succBlocks.size > 1) "branch"
                           else if (succBlocks.isEmpty) "exit"
                           else "flow"

      val terminatorObj = ujson.Obj("kind" -> terminatorKind)

      if (terminatorKind == "branch") {
        val (branchType, predicate) = detectBranchInfo(blockNodes, succMap, connectedNodeIdSet)
        val succBlockIdxs           = succBlockMap(idx).toList
        val labelMap                = computeBranchEdgeLabels(
          branchType, succBlockIdxs, blockNodeLists, connectedNodesById
        )

        // Store edge labels for use in edge building and blockPaths
        succBlockIdxs.foreach { toIdx =>
          edgeLabels((idx, toIdx)) = (labelMap.getOrElse(toIdx, s"branch_?"), predicate)
        }

        if (branchType != "FLOW" && branchType != "UNKNOWN" && branchType != "NONE") {
          terminatorObj("type") = branchType
        }
        predicate.foreach(p => terminatorObj("predicate") = p)
        val branchesArr = succBlockIdxs
          .sortBy(toIdx => labelMap.getOrElse(toIdx, ""))
          .map { toIdx =>
            ujson.Obj(
              "to"    -> blockStructure.blockName(toIdx),
              "label" -> labelMap.getOrElse(toIdx, s"branch_?")
            )
          }
        terminatorObj("branches") = ujson.Arr(branchesArr: _*)
      }

      val blockObj = ujson.Obj(
        "id"          -> blockStructure.blockName(idx),
        "lineStart"   -> lineStart,
        "lineEnd"     -> lineEnd,
        "nodeCount"   -> nodeIds.size,
        "isEntry"     -> entryBlockIdxSet.contains(idx),
        "isExit"      -> exitBlockIdxSet.contains(idx),
        "isBranch"    -> branchBlockIdxSet.contains(idx),
        "isJoin"      -> joinBlockIdxSet.contains(idx),
        "instructions" -> ujson.Arr(instructions.map(ujson.Str(_)): _*),
        "types"       -> ujson.Arr(types.map(ujson.Str(_)): _*),
        "identifiers" -> ujson.Arr(identifiers.map(ujson.Str(_)): _*),
        "predBlockIds" -> ujson.Arr(predBlocks.map(ujson.Str(_)): _*),
        "succBlockIds" -> ujson.Arr(succBlocks.map(ujson.Str(_)): _*),
        "terminator"  -> terminatorObj
      )

      if (includeBlockNodeDetails) {
        val predMap = cfgMaps.predMap
        val nodeDetails = limitedNodes.map { node =>
          ujson.Obj(
            "id"           -> node.id,
            "label"        -> Option(node.label).getOrElse(""),
            "line"         -> node.lineNumber.getOrElse(-1),
            "order"        -> node.order,
            "code"         -> Option(node.code).getOrElse(""),
            "typeFullName" -> nodeTypeOf(node),
            "predNodeIds"  -> ujson.Arr(predMap.getOrElse(node.id, Nil).map(ujson.Num(_)): _*),
            "succNodeIds"  -> ujson.Arr(succMap.getOrElse(node.id, Nil).map(ujson.Num(_)): _*)
          )
        }
        blockObj("nodes") = ujson.Arr(nodeDetails: _*)
      }

      blockObj
    }

    BlockMetadataResult(cfgBlocks, edgeLabels.toMap)
  }

  // -------------------------------------------------
  // Main logic - CFG block analysis (Step 4: Generate Block-level Paths)
  // -------------------------------------------------

  /**
   * 블록 그래프에서 DFS로 실행 경로 샘플 생성.
   * 각 경로: blocks (block ID 순서) + conditions (각 step의 edge label, entry는 null)
   */
  private def generateBlockPaths(
    blockStructure: BasicBlockStructure,
    edgeLabels: Map[(Int, Int), (String, Option[String])],
    maxPaths: Int,
    maxDepth: Int
  ): List[ujson.Obj] = {
    val effectiveMax   = math.min(math.max(maxPaths, 1), CfgAnalysisLimits.MAX_BLOCK_PATHS)
    val effectiveDepth = math.min(math.max(maxDepth, 1), CfgAnalysisLimits.MAX_BLOCK_PATH_DEPTH)

    val entryIdxs = blockStructure.entryBlockIdxSet.toList.sorted
    val paths     = mutable.ListBuffer.empty[(List[String], List[ujson.Value])]

    def dfs(
      currentIdx: Int,
      path: List[String],
      conditions: List[ujson.Value],
      visited: Set[Int]
    ): Unit = {
      if (paths.size >= effectiveMax) return
      val blockId  = blockStructure.blockName(currentIdx)
      val newPath  = path :+ blockId
      val succs    = blockStructure.succBlockMap(currentIdx).toList.sorted

      if (newPath.size >= effectiveDepth || succs.isEmpty) {
        paths += ((newPath, conditions))
      } else {
        val unvisited = succs.filterNot(visited.contains)
        if (unvisited.isEmpty) {
          paths += ((newPath, conditions))
        } else {
          unvisited.foreach { toIdx =>
            val labelVal: ujson.Value = edgeLabels.get((currentIdx, toIdx)) match {
              case Some((lbl, _)) => ujson.Str(lbl)
              case None           => ujson.Str("flow")
            }
            dfs(toIdx, newPath, conditions :+ labelVal, visited + currentIdx)
          }
        }
      }
    }

    entryIdxs.foreach { startIdx =>
      dfs(startIdx, Nil, Nil, Set.empty)
    }

    paths.toList.map { case (blockIds, conditions) =>
      // conditions has one fewer element than blockIds (no incoming edge for entry block)
      val paddedConditions: List[ujson.Value] = ujson.Null :: conditions.toList
      ujson.Obj(
        "blocks"     -> ujson.Arr(blockIds.map(ujson.Str(_)): _*),
        "conditions" -> ujson.Arr(paddedConditions: _*)
      )
    }
  }

  // -------------------------------------------------
  // Main logic - CFG block analysis (Main Entry Point)
  // -------------------------------------------------

  /**
   * 메서드의 CFG를 분석하여 JSON 리포트를 생성하는 메인 함수
   */
  def computeCfgReport(
    method: Method,
    maxNodesPerBlock: Int,
    maxInstructionsPerBlock: Int,
    includeBlockNodeDetails: Boolean,
    maxBlockPaths: Int,
    maxBlockPathDepth: Int
  )(implicit cpg: Cpg): ujson.Obj = {
    // Step 1: CFG 노드 및 엣지 매핑 구축
    val cfgMaps = buildCfgMaps(method)

    // Step 2: Basic Block 식별
    val blockStructure = identifyBasicBlocks(cfgMaps)

    // Step 3: 블록 메타데이터 추출 (통합 cfg 형식)
    val blockResult = extractBlockMetadata(
      cfgMaps,
      blockStructure,
      maxNodesPerBlock,
      maxInstructionsPerBlock,
      includeBlockNodeDetails
    )

    // Step 4: 블록 수준 경로 샘플 생성
    val blockPaths = generateBlockPaths(
      blockStructure, blockResult.edgeLabels, maxBlockPaths, maxBlockPathDepth
    )

    // Step 5: cfg edges (label + optional predicate)
    val cfgEdges = blockStructure.sortedBlockEdgesIdx.map { case (from, to) =>
      val (label, predicateOpt) =
        blockResult.edgeLabels.getOrElse((from, to), ("flow", None))
      val edgeObj = ujson.Obj(
        "from"  -> blockStructure.blockName(from),
        "to"    -> blockStructure.blockName(to),
        "label" -> label
      )
      predicateOpt.foreach(p => edgeObj("predicate") = p)
      edgeObj
    }

    // Step 6: cfgSummary - semantic flags, no raw node ID arrays
    val entryBlockIdxSet  = blockStructure.entryBlockIdxSet
    val exitBlockIdxSet   = blockStructure.exitBlockIdxSet
    val branchBlockIdxSet = blockStructure.branchBlockIdxSet

    val entryBlockIds = entryBlockIdxSet.toList.sorted.map(i => blockStructure.blockName(i))
    val exitBlockIds  = exitBlockIdxSet.toList.sorted.map(i => blockStructure.blockName(i))

    val loopCount = cfgMaps.connectedNodesById.values.count {
      case cs: ControlStructure =>
        val t = Option(cs.controlStructureType).getOrElse("")
        t == "WHILE" || t == "FOR" || t == "DO"
      case _ => false
    }

    val cfgSummary = ujson.Obj(
      "nodeCount"      -> cfgMaps.cfgNodes.size,
      "edgeCount"      -> cfgMaps.edgeCount,
      "blockCount"     -> blockStructure.blockNodeLists.size,
      "blockEdgeCount" -> blockStructure.sortedBlockEdgesIdx.size,
      "branchCount"    -> branchBlockIdxSet.size,
      "loopCount"      -> loopCount,
      "hasLoop"        -> detectHasLoop(cfgMaps.connectedNodesById),
      "hasTryCatch"    -> detectHasTryCatch(cfgMaps.connectedNodesById),
      "entryBlockId"   -> entryBlockIds.headOption.map(ujson.Str(_)).getOrElse(ujson.Null),
      "exitBlockIds"   -> ujson.Arr(exitBlockIds.map(ujson.Str(_)): _*)
    )

    // Step 7: Assemble final report
    ujson.Obj(
      "method" -> ujson.Obj(
        "id"        -> method.id,
        "name"      -> method.name,
        "fullName"  -> method.fullName,
        "signature" -> method.signature,
        "location"  -> methodLocation(method),
        "modifiers" -> ujson.Arr(method.modifier.modifierType.l.map(ujson.Str(_)): _*)
      ),
      "frameworkTypes" -> ujson.Arr(frameworkTypesOf(method).map(ujson.Str(_)): _*),
      "cfgSummary"     -> cfgSummary,
      "cfg" -> ujson.Obj(
        "entryBlockId" -> entryBlockIds.headOption.map(ujson.Str(_)).getOrElse(ujson.Null),
        "exitBlockIds" -> ujson.Arr(exitBlockIds.map(ujson.Str(_)): _*),
        "blocks"       -> ujson.Arr(blockResult.cfgBlocks: _*),
        "edges"        -> ujson.Arr(cfgEdges: _*)
      ),
      "blockPaths" -> ujson.Arr(blockPaths: _*)
    )
  }

  def saveDotIfRequested(dotCfg: String, outputDotPath: String): Unit = {
    if (outputDotPath.nonEmpty && dotCfg.nonEmpty) {
      writeUtf8(outputDotPath, dotCfg)
      println(s"[+] Saved dot CFG to $outputDotPath")
    }
  }
}

@main def analyzeMethodCfg(
  cpgPath: String,
  methodId: Long = -1L,
  methodFullName: String = "",
  methodName: String = "",
  maxNodesPerBlock: Int = 80,
  maxInstructionsPerBlock: Int = 12,
  includeBlockNodeDetails: Boolean = false,
  maxBlockPaths: Int = 8,
  maxBlockPathDepth: Int = 20,
  outputPath: String = "method-cfg.json",
  outputDotPath: String = ""
): Unit = {
  import MethodCfgAnalysis.*

  implicit val cpg: Cpg = loadCpg(cpgPath)

  val target = resolveTargetMethod(
    methodId      = methodId,
    methodFullName = methodFullName,
    methodName    = methodName
  )

  val report = computeCfgReport(
    method                 = target,
    maxNodesPerBlock       = maxNodesPerBlock,
    maxInstructionsPerBlock = maxInstructionsPerBlock,
    includeBlockNodeDetails = includeBlockNodeDetails,
    maxBlockPaths          = maxBlockPaths,
    maxBlockPathDepth      = maxBlockPathDepth
  )

  val root = ujson.Obj(
    "meta" -> ujson.Obj(
      "cpgPath" -> cpgPath,
      "selector" -> ujson.Obj(
        "methodId"      -> methodId,
        "methodFullName" -> methodFullName,
        "methodName"    -> methodName
      ),
      "selectedMethod" -> ujson.Obj(
        "methodId"       -> target.id,
        "methodFullName" -> target.fullName,
        "location"       -> s"${target.filename}:${target.lineNumber.getOrElse(-1)}"
      )
    ),
    "analysis" -> report
  )

  val json = ujson.write(root, indent = 2)
  writeUtf8(outputPath, json)
  println(s"[+] Saved method CFG analysis to $outputPath")

  val dot = Try(target.dotCfg.headOption.getOrElse("")).getOrElse("")
  saveDotIfRequested(dot, outputDotPath)

  println(s"[+] Target method: ${target.fullName} @ ${target.filename}:${target.lineNumber.getOrElse(-1)}")
}

// 실행 예시:
// /path/to/joern/joern-cli/joern \
//   --script /path/to/joern/MethodCfgAnalysis.sc \
//   --param cpgPath=/path/to/joern/joern-cli/alarmclock.cpg \
//   --param "methodFullName=com.better.alarm.ui.list.AlarmListAdapter.getView:android.view.View(int,android.view.View,android.view.ViewGroup)" \
//   --param outputPath=/path/to/joern/method-cfg.getView.json
//
// 샘플 출력: method-cfg.getView.json
