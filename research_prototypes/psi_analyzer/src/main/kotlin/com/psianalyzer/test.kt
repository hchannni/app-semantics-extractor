// 'embeddable' 버전에 리패키징된 IntelliJ PSI 클래스들을 import합니다.
import org.jetbrains.kotlin.com.intellij.openapi.project.Project
import org.jetbrains.kotlin.com.intellij.openapi.util.Disposer
import org.jetbrains.kotlin.com.intellij.psi.PsiManager
import org.jetbrains.kotlin.com.intellij.testFramework.LightVirtualFile

// Kotlin 컴파일러 API들을 import합니다.
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


fun main() {
    // 1. 컴파일러 환경 초기화
    println("--- 1. 컴파일러 환경 초기화 ---")
    val disposable = Disposer.newDisposable()   // 메모리 관리 및 자원 해제를 위한 도구
    val configuration = CompilerConfiguration()
    val configFiles = EnvironmentConfigFiles.JVM_CONFIG_FILES

    // TopDownAnalyzerFacadeForJVM가 null을 반환하지 않도록 필수 정보들을 채워 넣는다.
    // - 분석할 코드 덩어리 (모듈)에 임의의 이름을 붙여준다.
    configuration.put(CommonConfigurationKeys.MODULE_NAME, "my-analysis-module")

    // - 컴파일러의 에러 리포터를 설정한다.
    configuration.put(
        CLIConfigurationKeys.MESSAGE_COLLECTOR_KEY,
        PrintingMessageCollector(System.err, MessageRenderer.PLAIN_FULL_PATHS, true)
    )

    // 역할 (가장 중요): 우리 코드에 println, String처럼 기본 내장(built-in) 클래스가 등장할 때, 컴파일러가 "이것들이 무엇인지" 알 수 있도록 JDK의 위치를 알려준다.
    // System.getProperty("java.home")을 통해 현재 실행 중인 Java의 라이브러리(.jar 파일) 경로를 통째로 클래스패스에 추가한다.
    configuration.addJvmSdkRoots(listOf(File(System.getProperty("java.home"))))

    // 실제로 "가상 환경"을 부트스트랩하는 지점. (가상 IntelliJ 환경을 띄운다. 컴파일러의 프론트엔드를 실행시키기 위해)
    // 우리가 설정한 configuration과 메모리 관리를 위한 disposable 객체를 사용 -> PSI 파싱과 시멘틱 분석에 필요한 모든 서비스를 메모리에 로드한다.
    val env = KotlinCoreEnvironment.createForProduction(
        disposable,
        configuration,
        configFiles
    )
    val project: Project = env.project  // env(바로 위)가 생성한 가상 환경의 최상위 객체인 Project를 꺼낸다.
    println("--- 컴파일러 환경 초기화 완료 ---")

    // 2. 분석할 예제 소스 코드 정의 및 PSI 파싱
    println("--- 2. PSI 트리 파싱 ---")
    val exampleCode = """
        package com.example

        class Greeter(val name: String) {
            fun greet() {
                println("Hello, " + this.name)
            }
        }

        fun main() {
            val greeter = Greeter("World")
            greeter.greet()
        }
    """.trimIndent()
    val file = LightVirtualFile("Example.kt", KotlinLanguage.INSTANCE, exampleCode)
    val psiFile = PsiManager.getInstance(project).findFile(file) as? KtFile

    if (psiFile == null) {
        println("PSI 파일 파싱 실패!")
        return
    } else {
        println("--- PSI 트리 파싱 완료 ---")
        println("--- 파싱된 PSI 트리 구조 ---")
        psiFile.accept(object : KtTreeVisitorVoid() {
            // 이 visitElement는 모든 노드를 방문할 때마다 호출됩니다.
            override fun visitElement(element: PsiElement) {
                super.visitElement(element) // 자식 노드를 계속 방문하기 위해 super 호출

                // 노드의 깊이를 간단히 표현하기 위해 부모 노드 개수 세기
                val depth = element.parents.count()
                val indent = "  ".repeat(depth)

                // 노드 타입과 텍스트 일부 출력
                val nodeType = element.node.elementType.toString()
                val text = element.text.lines().firstOrNull()?.take(30)?.trim() ?: ""

                println("$indent- [${nodeType}] $text")
            }
        })
    }

    println("--- 3. 의미 분석 실행 (BindingContext 생성) ---")
    val trace = CliBindingTrace()
    val files = listOf(psiFile)

    val analysisResult: AnalysisResult = TopDownAnalyzerFacadeForJVM.analyzeFilesWithJavaIntegration(   // K1 컴파일러의 분석 엔진 실행
        project,
        files,
        trace,
        configuration,
        { _ -> PackagePartProvider.Empty }
    )

    val bindingContext = analysisResult.bindingContext  // "정답지(BindingContext)" 획득!

    if (analysisResult.isError()) {
        println("컴파일 분석 중 에러 발생:")
        trace.bindingContext.diagnostics.forEach { println(it) }
    }

    println("--- 4. BindingContext에서 정보 추출 ---")
    psiFile.accept(object : KtTreeVisitorVoid() {

        // (1) 프로퍼티(변수) 선언부 방문
        override fun visitProperty(property: KtProperty) {
            super.visitProperty(property)

            if (property.initializer != null) {
                println("\n[발견] '${property.name}' 변수 선언 (offset: ${property.startOffset})")

                // 'property.initializer'가 null이 아님을 위에서 확인
                val type = bindingContext.getType(property.initializer!!)
                println("  (1) 타입 추론: $type")
            }
        }

        // (2) 함수 호출 표현식 방문
        override fun visitCallExpression(expression: KtCallExpression) {
            super.visitCallExpression(expression)

            if (expression.calleeExpression?.text == "greet") {
                println("\n[발견] 'greet()' 함수 호출 (offset: ${expression.startOffset})")

                val call = expression.getCall(bindingContext)
                val resolvedCall = bindingContext[BindingContext.RESOLVED_CALL, call]
                if (resolvedCall != null) {
                    val functionFqName = resolvedCall.resultingDescriptor.fqNameOrNull()
                    println("  (2) 호출 해결: $functionFqName")
                }
            }
        }

        // (3) 참조 표현식(변수 사용처) 방문
        override fun visitReferenceExpression(expression: KtReferenceExpression) {
            super.visitReferenceExpression(expression)

            if (expression.text == "name") {
                println("\n[발견] 'name' 프로퍼티 사용 (offset: ${expression.startOffset})")

                val targetDescriptor = bindingContext[BindingContext.REFERENCE_TARGET, expression]
                val targetPsi = targetDescriptor?.toSourceElement?.getPsi()     // 'getPsi()'를 사용해 Descriptor를 'PsiElement'로 변환해야 합니다.

                if (targetPsi != null) {
                    // 이제 'targetPsi'는 PsiElement이므로 '.containingFile'에 접근할 수 있습니다.
                    val targetFile = targetPsi.containingFile.name
                    val targetOffset = targetPsi.startOffset
                    println("  (3) 참조 해결: $targetFile 파일의 $targetOffset 오프셋 (val name: String)")
                }
            }
        }
    })

    println("\n--- 분석 완료 ---")

    // 환경 정리
    disposable.dispose()
}