package com.psianalyzer

import org.jetbrains.kotlin.com.intellij.openapi.project.Project
import org.jetbrains.kotlin.com.intellij.openapi.util.Disposer
import org.jetbrains.kotlin.com.intellij.psi.PsiManager
import org.jetbrains.kotlin.com.intellij.testFramework.LightVirtualFile
import org.jetbrains.kotlin.analyzer.AnalysisResult
import org.jetbrains.kotlin.cli.common.messages.MessageRenderer
import org.jetbrains.kotlin.cli.common.messages.PrintingMessageCollector
import org.jetbrains.kotlin.cli.jvm.compiler.EnvironmentConfigFiles
import org.jetbrains.kotlin.cli.jvm.compiler.KotlinCoreEnvironment
import org.jetbrains.kotlin.cli.jvm.compiler.TopDownAnalyzerFacadeForJVM
import org.jetbrains.kotlin.cli.jvm.config.addJvmSdkRoots
import org.jetbrains.kotlin.cli.common.CLIConfigurationKeys
import org.jetbrains.kotlin.cli.jvm.compiler.CliBindingTrace
import org.jetbrains.kotlin.com.intellij.psi.PsiElement
import org.jetbrains.kotlin.config.CommonConfigurationKeys
import org.jetbrains.kotlin.config.CompilerConfiguration
import org.jetbrains.kotlin.load.kotlin.PackagePartProvider
import org.jetbrains.kotlin.idea.KotlinLanguage
import org.jetbrains.kotlin.load.kotlin.toSourceElement
import org.jetbrains.kotlin.psi.* // KtFile, KtVisitorVoid, KtProperty 등
import org.jetbrains.kotlin.psi.psiUtil.parents
import org.jetbrains.kotlin.psi.psiUtil.startOffset
import org.jetbrains.kotlin.resolve.BindingContext
import org.jetbrains.kotlin.resolve.calls.util.getCall
import org.jetbrains.kotlin.resolve.descriptorUtil.fqNameOrNull
import org.jetbrains.kotlin.resolve.source.getPsi
import java.io.File


/**
 * State Predicate 추출 통합 파이프라인
 * 
 * C1 전략의 1~3단계를 순차적으로 실행하여 최종 State Predicate를 추출합니다.
 */
fun main() {
    println("=================================================")
    println("  State Predicate 자동 추출 파이프라인")
    println("=================================================\n")
    
    // 1단계: 환경 초기화
    println("--- 1단계: 컴파일러 환경 초기화 ---")
    val (project, bindingContext, psiFiles, disposable) = initializeEnvironment() ?: return
    println("✓ 총 ${psiFiles.size}개의 Kotlin 파일 로드 완료\n")
    
    try {
        // 2단계: State Hub 식별
        println("--- 2단계: State Hub 식별 (ViewModel, Store 탐지) ---")
        val stateHubIdentifier = StateHubIdentifier(project, bindingContext)
        val analysisResult = stateHubIdentifier.identifyStateHubs(psiFiles)
        println("✓ ${analysisResult.stateHubs.size}개의 State Hub 발견")
        analysisResult.stateHubs.forEach { hub ->
            println("  - ${hub.className} (${hub.level}): ${hub.stateStreams.size}개 StateStream")
        }
        println()
        
        // 3단계: Raw State 추출
        println("--- 3단계: Raw State 추출 (원시 상태 타입 재귀 분석) ---")
        val rawStateExtractor = RawStateExtractor(project, bindingContext)
        val rawStates = rawStateExtractor.extractRawStates(analysisResult.stateHubs, psiFiles)
        println("✓ ${rawStates.size}개의 Raw State 추출")
        rawStates.forEach { rawState ->
            val typeNodeCount = countTypeNodes(rawState.rootType)
            println("  - ${rawState.streamName} (${rawState.streamType}): ${typeNodeCount}개 노드")
        }
        println()
        
        // 4단계: Layout 메타데이터 수집 (XML → ViewMetadata)
        println("--- 4단계: Layout 메타데이터 수집 ---")
        val layoutRoot = File("../samples/SimpleAlarmClock/app-source-code/app/src/main/res/layout")
        val layoutFiles = if (layoutRoot.exists()) {
            layoutRoot.walkTopDown().filter { it.isFile && it.extension == "xml" }.toList()
        } else {
            emptyList()
        }
        if (layoutFiles.isEmpty()) {
            println("! 레이아웃 디렉터리를 찾지 못했거나 XML 파일이 없습니다.")
        } else {
            println("✓ ${layoutFiles.size}개의 레이아웃 XML 발견")
        }
        val xmlLayoutParser = XmlLayoutParser()
        val layoutMetadata = if (layoutFiles.isEmpty()) {
            emptyMap()
        } else {
            xmlLayoutParser.parseLayoutFiles(layoutFiles)
        }
        println("✓ ${layoutMetadata.size}개의 ViewMetadata 수집\n")

        // 5단계: View Inventory 수집
        println("--- 5단계: View Inventory 수집 ---")
        val viewInventoryCollector = ViewInventoryCollector(bindingContext, layoutMetadata)
        val viewInstances = viewInventoryCollector.collect(psiFiles)
        println("✓ ${viewInstances.size}개의 View 인스턴스 수집\n")
        
        // 6단계: State-View 동시 사용 분석
        println("--- 6단계: State-View 동시 사용 분석 ---")
        val stateViewMapper = StateViewMapper(bindingContext, rawStateExtractor)
        val stateViewMappings = stateViewMapper.analyzeStateViewMappings(psiFiles, rawStates, viewInstances)
        println("✓ ${stateViewMappings.size}개의 State-View 매핑 후보 발견\n")
        
        // 7단계: UI Event Source 탐지
        println("--- 7단계: UI Event Source 탐지 (UI → 상태) ---")
        val eventSourceFinder = UiEventSourceFinder(project, bindingContext)
        val eventSources = eventSourceFinder.findEventSources(psiFiles)
        println("✓ ${eventSources.size}개의 이벤트 소스 발견")
        println()
        
//        // 6단계: State Predicate 정제
//        println("--- 6단계: State Predicate 정제 (필터링 + 시맨틱 강화) ---")
//        val refiner = StatePredicateRefiner(project, bindingContext)
//        val refinedPredicates = refiner.refinePredicates(rawStates, bindings, eventSources)
//        println("✓ ${refinedPredicates.size}개의 최종 State Predicate 확정")
//        refinedPredicates.forEach { refined ->
//            val exposed = refined.uiExposure.exposedVariables.size
//            val triggerable = refined.triggerability.triggerableVariables.size
//            println("  - ${refined.predicate.name}: ${refined.predicate.variables.size}개 변수 " +
//                    "(UI 노출: $exposed, Trigger 가능: $triggerable)")
//        }
//        println()
        
        // 8단계: 결과 출력
        println("--- 8단계: 결과 JSON 출력 ---")
        val outputDir = File("output")
        outputDir.mkdirs()
        
        // State Hubs JSON
        File(outputDir, "state_hubs.json").writeText(stateHubIdentifier.toJson(analysisResult))
        println("✓ state_hubs.json 저장 완료")
        
        // Raw States JSON
        File(outputDir, "raw_states.json").writeText(rawStateExtractor.toJson(rawStates))
        println("✓ raw_states.json 저장 완료 (원시 상태 정보)")

        // Layout Metadata JSON
        if (layoutMetadata.isNotEmpty()) {
            File(outputDir, "layout_metadata.json")
                .writeText(xmlLayoutParser.toJson(layoutMetadata))
            println("✓ layout_metadata.json 저장 완료 (총 ${layoutMetadata.size}개 뷰)")
        } else {
            println("! layout_metadata.json 생성 건너뜀 (수집된 메타데이터 없음)")
        }
        
        // View Inventory JSON
        File(outputDir, "view_inventory.json").writeText(viewInventoryCollector.toJson(viewInstances))
        println("✓ view_inventory.json 저장 완료 (View 인벤토리 정보)")
        
        // State-View Mapping JSON
        File(outputDir, "state_view_mappings.json").writeText(stateViewMapper.toJson(stateViewMappings))
        println("✓ state_view_mappings.json 저장 완료 (State-View 매핑 정보)")

        // Event Sources JSON
        File(outputDir, "event_sources.json").writeText(eventSourceFinder.toJson(eventSources))
        println("✓ event_sources.json 저장 완료")
        
//        // Refined Predicates JSON (최종 결과)
//        File(outputDir, "state_predicates.json").writeText(refiner.toJson(refinedPredicates))
//        println("✓ state_predicates.json 저장 완료 (최종 결과)")
        
        println("\n=================================================")
        println("  분석 완료! output/ 디렉토리를 확인하세요.")
        println("=================================================")
        
    } finally {
        disposable.dispose()
    }
}

/**
 * 컴파일러 환경 초기화 및 파일 로드
 */
private fun initializeEnvironment(): Tuple4<Project, BindingContext, List<KtFile>, org.jetbrains.kotlin.com.intellij.openapi.Disposable>? {
    val disposable = Disposer.newDisposable()
    val configuration = CompilerConfiguration()
    val configFiles = EnvironmentConfigFiles.JVM_CONFIG_FILES

    configuration.put(CommonConfigurationKeys.MODULE_NAME, "state-predicate-analyzer")
    configuration.put(
        CLIConfigurationKeys.MESSAGE_COLLECTOR_KEY,
        PrintingMessageCollector(System.err, MessageRenderer.PLAIN_FULL_PATHS, true)
    )

    val jdkHome = File(System.getProperty("java.home"))
    val androidJar = File("libs/android.jar")
    configuration.addJvmSdkRoots(listOf(jdkHome, androidJar))

    val env = KotlinCoreEnvironment.createForProduction(disposable, configuration, configFiles)
    val project: Project = env.project

    // 소스 코드 로드
    val sourceRootPath = "../samples/SimpleAlarmClock/app-source-code/app/src/main/java"
    val sourceRoot = File(sourceRootPath)

    if (!sourceRoot.exists()) {
        println("!! 소스 코드 디렉터리를 찾을 수 없습니다: ${sourceRoot.absolutePath}")
        disposable.dispose()
        return null
    }

    val psiManager = PsiManager.getInstance(project)
    val psiFiles = mutableListOf<KtFile>()

    sourceRoot.walkTopDown().forEach { file ->
        if (file.isFile && file.extension == "kt") {
            val fileContent = file.readText()
            val virtualFile = LightVirtualFile(file.name, KotlinLanguage.INSTANCE, fileContent)
            val psiFile = psiManager.findFile(virtualFile) as? KtFile
            if (psiFile != null) {
                psiFiles.add(psiFile)
            }
        }
    }

    if (psiFiles.isEmpty()) {
        println("!! 파싱할 .kt 파일을 찾지 못했습니다.")
        disposable.dispose()
        return null
    }

    // BindingContext 생성
    val trace = CliBindingTrace()
    val analysisResult: AnalysisResult = TopDownAnalyzerFacadeForJVM.analyzeFilesWithJavaIntegration(
        project,
        psiFiles,
        trace,
        configuration,
        { _ -> PackagePartProvider.Empty }
    )

    val bindingContext = analysisResult.bindingContext

    if (analysisResult.isError()) {
        println("!! 경고: 컴파일 분석 중 일부 에러 발생 (계속 진행)")
    }

    return Tuple4(project, bindingContext, psiFiles, disposable)
}

/**
 * State Hub에서 상태 변수 이름들을 추출합니다.
 * 
 * UiBindingAnalyzer에 전달할 변수 이름 목록을 생성합니다.
 */
/**
 * TypeNode 트리의 총 노드 개수를 계산합니다.
 */
fun countTypeNodes(node: RawStateExtractor.TypeNode): Int {
    return when (node) {
        is RawStateExtractor.PrimitiveTypeNode -> 1
        is RawStateExtractor.ExternalTypeNode -> 1
        is RawStateExtractor.ComplexTypeNode -> {
            1 + node.properties.sumOf { countTypeNodes(it) }
        }
    }
}

/**
 * 4개 요소를 담는 Tuple (Kotlin 표준 라이브러리에 없음)
 */
data class Tuple4<A, B, C, D>(val first: A, val second: B, val third: C, val fourth: D)