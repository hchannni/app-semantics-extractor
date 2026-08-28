import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import scala.collection.mutable

object ContextCallbackResolver {
  import ContextSlicerModel.*

  // Internal BFS result — converted to CallPath before returning.
  private case class ResolvedPath(
    rootCallback: String,
    callbackKind: String,
    resolutionDepth: Int,
    relation: String,
    path: List[String]
  )

  private val callbackFrameworkPrefixes = List(
    "android.",
    "androidx.",
    "com.google.android.material."
  )
  private val traversalStopPrefixes = List(
    "android.",
    "androidx.",
    "java.",
    "kotlin.",
    "com.google.android.material."
  )

  private val lifecycleCallbackHints = Set(
    "oncreate",
    "oncreateview",
    "onstart",
    "onresume",
    "onpause",
    "onstop",
    "ondestroy",
    "onrestart",
    "onnewintent",
    "onsaveinstancestate",
    "onrestoreinstancestate",
    "onactivityresult",
    "oncreatecontextmenu",
    "oncreateoptionsmenu",
    "onprepareoptionsmenu",
    "onoptionsitemselected"
  )

  private val adapterCallbackNames = Set(
    "getview",
    "onbindviewholder",
    "oncreateviewholder"
  )

  private case class CallbackTraversalNode(
    method: Method,
    depth: Int,
    relation: String,
    path: List[String]
  )

  private def methodFrameworkTypes(method: Method)(implicit cpg: Cpg): List[String] =
    method.definingTypeDecl.headOption.toList.flatMap { td =>
      val direct = Option(td.fullName).toList
      val inherited = td.inheritsFromTypeFullName.l.flatMap(Option(_))
      val bases = td.baseTypeDecl.fullName.l.flatMap(Option(_))
      (direct ++ inherited ++ bases).distinct
    }

  private def methodHasFrameworkType(method: Method)(implicit cpg: Cpg): Boolean =
    methodFrameworkTypes(method).exists { t =>
      callbackFrameworkPrefixes.exists(prefix => t.startsWith(prefix))
    }

  private def methodHasPrivateModifier(method: Method): Boolean =
    method.modifier.modifierType.l.exists(_.equalsIgnoreCase("PRIVATE"))

  private def methodHasOverrideMarker(method: Method): Boolean = {
    val codeHasOverride = Option(method.code).exists(_.toLowerCase.contains("override"))
    val overrideAnnotation = method.annotation.name.l.exists(_.equalsIgnoreCase("Override"))
    codeHasOverride || overrideAnnotation
  }

  private def callbackKindForMethod(method: Method)(implicit cpg: Cpg): Option[String] = {
    val methodName = Option(method.name).getOrElse("").toLowerCase
    if (methodName.isEmpty) return None
    if (methodHasPrivateModifier(method)) return None

    // Kotlin/Java CPG에서 상속 정보가 누락되는 경우가 있어 adapter callbacks는 이름 기반으로 우선 허용한다.
    if (adapterCallbackNames.contains(methodName)) return Some("ADAPTER")

    if (!methodHasFrameworkType(method)) return None

    if (lifecycleCallbackHints.contains(methodName)) Some("LIFECYCLE")
    else if (methodName.startsWith("on")) Some("LISTENER")
    else if (methodHasOverrideMarker(method)) Some("FRAMEWORK_OVERRIDE")
    else None
  }

  private def isTraversalBoundary(fullName: String): Boolean =
    traversalStopPrefixes.exists(prefix => fullName.startsWith(prefix))

  private def isTraversableMethodFullName(fullName: String): Boolean =
    fullName.nonEmpty && !fullName.startsWith("<") && !isTraversalBoundary(fullName)

  private def isFileBacked(method: Method): Boolean = {
    val file = method.file.name.headOption.getOrElse("")
    file.nonEmpty && file != "<unknown>"
  }

  private def lambdaRegistrationHosts(
    lambdaMethod: Method,
    baseDepth: Int,
    basePath: List[String]
  )(implicit cpg: Cpg): List[CallbackTraversalNode] = {
    val lambdaFullName = Option(lambdaMethod.fullName).getOrElse("")
    if (lambdaFullName.isEmpty) return Nil
    if (!lambdaFullName.contains("<lambda>") && !lambdaFullName.contains("<anonymous>")) return Nil

    cpg.methodRef
      .methodFullNameExact(lambdaFullName)
      .l
      .flatMap { methodRef =>
        methodRef.inCall.headOption.toList.flatMap { call =>
          Option(call.method).toList.map { host =>
            val hostFullName = Option(host.fullName).getOrElse("")
            CallbackTraversalNode(
              method = host,
              depth = baseDepth + 1,
              relation = "LAMBDA_REGISTRATION_CHAIN",
              path = (basePath ++ List(hostFullName)).filter(_.nonEmpty)
            )
          }
        }
      }
      .filter(node =>
        Option(node.method.fullName).exists(isTraversableMethodFullName) && isFileBacked(node.method)
      )
      .distinctBy(node => node.method.id)
  }

  private def relationPriority(relation: String): Int =
    relation match {
      case "SELF" => 0
      case "LAMBDA_REGISTRATION_CHAIN" => 1
      case "CALLER_CHAIN" => 2
      case _ => 9
    }

  /** primary method → root callback까지의 backward 경로를 BFS로 추적한다.
    *
    * 목표: usage가 실행되는 호출 체인을 역방향으로 따라가며 Android 콜백까지 올라간다.
    * 예) Framework → onCreate → setupUI → configureButton → [primary]
    *     역방향: primary ← configureButton ← setupUI ← onCreate (root callback)
    *
    * BFS 선택 이유:
    * - visitKey=(methodId, relationKey)로 "처음 도달한 경로"만 기록한다.
    * - BFS는 depth 순으로 처리하므로 짧은 경로를 먼저 발견한다.
    * - DFS면 긴 경로를 먼저 기록해, 짧은 경로가 영원히 기록되지 않을 수 있다.
    *
    * 탐색 전략:
    * 1) Lambda Registration Chain: 람다/익명 클래스인 경우 methodRef.inCall로 등록하는 host 메서드 탐색
    * 2) Caller Chain: cpg.call.methodFullNameExact(callee).method로 "이 메서드를 호출하는 caller" 탐색
    *
    * 결과 정리:
    * - root callback이 n개면 n개 경로 모두 반환 (각 root당 1개).
    * - 같은 root callback에 여러 경로가 있으면 groupBy 후 depth·relation 우선순위로 대표 1개만 선택.
    *
    * root callback 판별: callbackKindForMethod (LIFECYCLE, LISTENER, ADAPTER, FRAMEWORK_OVERRIDE)
    * 종료 조건: visited-set (사이클/재방문 차단), traversalStopPrefixes, isFileBacked
    */
  def resolveCallbackPaths(usageMethodFullName: String)(implicit cpg: Cpg): List[CallPath] = {
    if (usageMethodFullName.isEmpty) return Nil
    val startMethods = cpg.method.fullNameExact(usageMethodFullName).l.distinctBy(_.id)
    if (startMethods.isEmpty) return Nil

    val queue = mutable.Queue.empty[CallbackTraversalNode]
    // 1. primary method를 시작점으로 큐에 넣는다. relation=SELF는 경로의 시작점.
    startMethods.foreach { method =>
      val fullName = Option(method.fullName).getOrElse("")
      queue.enqueue(
        CallbackTraversalNode(
          method = method,
          depth = 0,
          relation = "SELF",
          path = List(fullName).filter(_.nonEmpty)
        )
      )
      // primary가 람다/익명 클래스면, 이 람다를 등록하는 host 메서드도 큐에 넣는다.
      lambdaRegistrationHosts(method, baseDepth = 0, basePath = List(fullName).filter(_.nonEmpty)).foreach(queue.enqueue(_))
    }

    val visited = mutable.Set.empty[(Long, String)]
    val resolved = mutable.ListBuffer.empty[ResolvedPath]

    while (queue.nonEmpty) {
      val current = queue.dequeue()
      val methodId = current.method.id
      val relationKey = current.relation
      val visitKey = (methodId, relationKey)
      if (!visited.contains(visitKey)) {
        visited += visitKey

        // 2. Lambda Registration Chain: 현재 메서드가 람다면 등록 host를 큐에 추가
        lambdaRegistrationHosts(current.method, current.depth, current.path).foreach(queue.enqueue(_))

        // 3. root callback 판별: 현재 메서드가 Android 콜백이면 기록
        callbackKindForMethod(current.method).foreach { callbackKind =>
          val fullName = Option(current.method.fullName).getOrElse("")
          if (fullName.nonEmpty) {
            resolved += ResolvedPath(
              rootCallback    = fullName,
              callbackKind    = callbackKind,
              resolutionDepth = current.depth,
              relation        = relationKey,
              path            = current.path
            )
          }
        }

        // 4. Caller Chain: "현재 메서드를 호출하는 caller"를 찾아 큐에 추가
        // 종료 조건은 visited-set이 보장하므로 depth 상한 불필요.
        {
          val nextRelation =
            if (relationKey == "SELF") "CALLER_CHAIN"
            else relationKey
          val calleeFullName = Option(current.method.fullName).getOrElse("")
          if (calleeFullName.nonEmpty) {
            cpg.call
              .methodFullNameExact(calleeFullName)
              .method
              .l
              .distinctBy(_.id)
              .foreach { caller =>
                val callerFullName = Option(caller.fullName).getOrElse("")
                if (isTraversableMethodFullName(callerFullName) && isFileBacked(caller)) {
                  queue.enqueue(
                    CallbackTraversalNode(
                      method   = caller,
                      depth    = current.depth + 1,
                      relation = nextRelation,
                      path     = current.path :+ callerFullName
                    )
                  )
                }
              }
          }
        }
      }
    }

    // 5. 같은 root callback에 대해 depth·relation 우선순위로 대표 경로 1개만 선택
    // 6. ResolvedPath → CallPath(direction=BACKWARD) 변환 후 반환
    resolved
      .groupBy(_.rootCallback)
      .values
      .flatMap { group =>
        group.sortBy(item => (item.resolutionDepth, relationPriority(item.relation))).headOption
      }
      .toList
      .sortBy(item => (item.resolutionDepth, relationPriority(item.relation), item.rootCallback))
      .map { r =>
        CallPath(
          direction    = "BACKWARD",
          pathMethods  = r.path,
          rootCallback = Some(r.rootCallback),
          callbackKind = Some(r.callbackKind),
          relation     = Some(r.relation)
        )
      }
  }
}
