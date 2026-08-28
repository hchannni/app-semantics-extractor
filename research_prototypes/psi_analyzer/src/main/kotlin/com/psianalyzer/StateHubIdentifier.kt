package com.psianalyzer

import org.jetbrains.kotlin.com.intellij.openapi.project.Project
import org.jetbrains.kotlin.psi.*
import org.jetbrains.kotlin.resolve.BindingContext
import org.jetbrains.kotlin.types.KotlinType
import org.jetbrains.kotlin.resolve.descriptorUtil.fqNameOrNull
import org.jetbrains.kotlin.psi.KtPsiUtil

/**
 * State Hub 식별기
 * 
 * 다층 State 아키텍처를 인식하고 핵심 상태 관리 지점을 찾습니다.
 * 
 * Level 1: Store 탐지 (여러 ViewModel이 DI로 공유하는 Singleton)
 * Level 2: ViewModel 역할 분석 (상태 소유자 vs 래퍼)
 * Level 3: Data Model 추출 (실제 State Predicate 후보)
 */
class StateHubIdentifier(
    private val project: Project,
    private val bindingContext: BindingContext
) {
    
    data class StateHub(
        val className: String,
        val filePath: String,
        val stateStreams: List<StateStreamInfo>,
        val level: HubLevel,
        val injectedIntoViewModels: List<String> = emptyList()
    )
    
    data class StateStreamInfo(
        val propertyName: String,
        val streamType: String, // StateFlow, LiveData, BehaviorSubject, etc.
        val dataType: String,
        val isDataClass: Boolean,
        val file: String
    )
    
    enum class HubLevel {
        CENTRAL_STORE,  // Level 1: 여러 ViewModel이 공유
        VIEW_MODEL,     // Level 2: 개별 ViewModel
        DATA_MODEL      // Level 3: Data class
    }

    data class AnalysisResult(
        val stateHubs: List<StateHub>,
        val viewModelInfos: List<ViewModelInfo>
    )
    
    data class ViewModelInfo(
        val className: String,
        val filePath: String,
        val injectedStores: List<String>,
        val ownedStateStreams: List<StateStreamInfo>
    )
    
    /**
     * 주어진 파일들에서 State Hub를 식별합니다.
     * 
     * @param ktFiles 분석할 Kotlin 파일들
     * @return 발견된 State Hub 목록 (우선순위 순으로 정렬)
     */
    fun identifyStateHubs(ktFiles: List<KtFile>): AnalysisResult {
        val allClasses = ktFiles.flatMap { extractClasses(it) }
        val viewModels = allClasses.filter { isViewModel(it) }
        val storeClasses = allClasses.filter { isStateStore(it) }
        
        val stateHubs = mutableListOf<StateHub>()
        val viewModelInfo = collectViewModelInfos(viewModels, ktFiles)
        
        // Level 1: Store 식별
        val stores = identifyStores(storeClasses, viewModels, ktFiles)
        stateHubs.addAll(stores)
        
        // Level 2: 독립적인 ViewModel(Store를 주입받지 않고 자체 state 소유한 경우) 분석
        val viewModelHubs = analyzeViewModels(viewModels, stores.map { it.className }, ktFiles)
        stateHubs.addAll(viewModelHubs)
        
        return AnalysisResult(
            viewModelInfos = viewModelInfo,
            stateHubs = stateHubs.sortedBy { it.level.ordinal }
        )
    }

    /**
     * ViewModel 정보를 수집합니다.
     * 
     * @param viewModels 분석할 ViewModel 목록
     * @param allFiles 검색할 KtFile파일 목록
     * @return ViewModel 정보 목록
     */
    private fun collectViewModelInfos(
        viewModels: List<KtClass>,
        allFiles: List<KtFile>
    ): List<ViewModelInfo> {
        val storeClasses = allFiles.flatMap { extractClasses(it) }.filter { isStateStore(it) }

        
        return viewModels.map { viewModel ->
            val constructor = viewModel.getPrimaryConstructor()
            val injectedStores = constructor?.valueParameters
                ?.mapNotNull { param ->
                    val typeName = param.typeReference?.text
                    ?.substringBefore('<')?.trim()
                    ?: return@mapNotNull null

                    if (typeName in storeClasses.map { it.name }) {
                        typeName
                    } else {
                        null
                    }
                }
                ?: emptyList()
            
            val ownedStateStreams = extractStateStreams(viewModel, allFiles)
            
            ViewModelInfo(
                className = viewModel.name ?: "Unknown",
                filePath = viewModel.containingKtFile.virtualFilePath,
                injectedStores,
                ownedStateStreams
            )
        }
    }
    
    /**
     * Level 1: Store 식별
     * 
     * ViewModel의 생성자 파라미터로 주입되는 Store를 찾습니다.
     */
    private fun identifyStores(
        storeClasses: List<KtClass>,
        viewModels: List<KtClass>,
        allFiles: List<KtFile>
    ): List<StateHub> {
        val storeInjectionMap = mutableMapOf<String, MutableList<String>>()
        
        // 각 ViewModel의 생성자 파라미터 분석
        viewModels.forEach { viewModel ->
            val constructor = viewModel.getPrimaryConstructor()
            constructor?.valueParameters?.forEach { param ->
                val paramType = param.typeReference?.text
                if (paramType != null) {
                    storeInjectionMap
                        .getOrPut(paramType) { mutableListOf() }
                        .add(viewModel.name ?: "Unknown")
                }
            }
        }
        
        val storeNames = storeInjectionMap.keys
        
        return storeClasses.filter { it.name in storeNames }.map { store ->
            val stateStreams = extractStateStreams(store, allFiles)
            StateHub(
                className = store.name ?: "Unknown",
                filePath = store.containingKtFile.name,
                stateStreams,
                level = HubLevel.CENTRAL_STORE,
                injectedIntoViewModels = storeInjectionMap[store.name] ?: emptyList()
            )
        }
    }
    
    /**
     * Level 2: ViewModel 분석
     * 
     * Store를 주입받는지, 아니면 직접 상태를 소유하는지 구분합니다.
     */
    private fun analyzeViewModels(
        viewModels: List<KtClass>,
        storeNames: List<String>,
        allFiles: List<KtFile>
    ): List<StateHub> {
        return viewModels.mapNotNull { viewModel ->
            val constructor = viewModel.getPrimaryConstructor()
            val injectedStores = constructor?.valueParameters
                ?.mapNotNull { it.typeReference?.text }
                ?.filter { it in storeNames }
                ?: emptyList()
            
            val ownedStateStreams = extractStateStreams(viewModel, allFiles)
            
            // Central Store를 주입받지 않고, 자체 StateFlow를 가지고 있으면 State Hub
            if (injectedStores.isEmpty() && ownedStateStreams.isNotEmpty()) {
                StateHub(
                    className = viewModel.name ?: "Unknown",
                    filePath = viewModel.containingKtFile.name,
                    stateStreams = ownedStateStreams,
                    level = HubLevel.VIEW_MODEL,
                    injectedIntoViewModels = emptyList()
                )
            } else {
                null
            }
        }
    }
    
    /**
     * 클래스에서 StateFlow, LiveData, BehaviorSubject를 추출합니다.
     */
    private fun extractStateStreams(ktClass: KtClass, allFiles: List<KtFile>): List<StateStreamInfo> {
        val stateStreams = mutableListOf<StateStreamInfo>()
        
        ktClass.getProperties().forEach { property ->
            val typeRef = property.typeReference?.text ?: return@forEach
            
            val streamType = when {
                // Kotlin Flow (가장 최신, 많이 사용됨)
                typeRef.contains("MutableStateFlow") -> "MutableStateFlow"
                typeRef.contains("StateFlow") -> "StateFlow"
                typeRef.contains("MutableSharedFlow") -> "MutableSharedFlow"
                typeRef.contains("SharedFlow") -> "SharedFlow"
                typeRef.contains("Flow") -> "Flow"
                
                // LiveData (전통적인 Android 방식)
                typeRef.contains("MutableLiveData") -> "MutableLiveData"
                typeRef.contains("LiveData") -> "LiveData"
                
                // RxJava Subject
                typeRef.contains("BehaviorSubject") -> "BehaviorSubject"
                typeRef.contains("PublishSubject") -> "PublishSubject"
                typeRef.contains("ReplaySubject") -> "ReplaySubject"
                
                // RxJava Observable 계열
                typeRef.contains("Observable") -> "Observable"
                typeRef.contains("Single") -> "Single"
                typeRef.contains("Completable") -> "Completable"
                typeRef.contains("Maybe") -> "Maybe"
                
                // Jetpack Compose
                typeRef.contains("MutableState") -> "MutableState"
                typeRef.contains("State<") -> "State"
                
                // Channel
                typeRef.contains("Channel") -> "Channel"
                
                else -> null
            }
            
            if (streamType != null) {
                // 타입 파라미터 추출: StateFlow<EditedAlarm> -> EditedAlarm
                val dataType = extractGenericType(typeRef)
                
                // data class 여부 확인
                val isDataClass = checkIfDataClass(dataType, allFiles)
                
                stateStreams.add(
                    StateStreamInfo(
                        propertyName = property.name ?: "unknown",
                        streamType = streamType,
                        dataType = dataType,
                        isDataClass = isDataClass,
                        file = ktClass.containingKtFile.name
                    )
                )
            }
        }
        
        return stateStreams
    }
    
    /**
     * 클래스가 ViewModel인지 확인합니다.
     */
    private fun isViewModel(ktClass: KtClass): Boolean {
        val superTypes = ktClass.superTypeListEntries.mapNotNull { it.text }
        return superTypes.any { type -> type.contains("ViewModel") } || 
            ktClass.name?.endsWith("ViewModel") == true
    }
    
    /**
     * 클래스가 State Store인지 확인합니다.
     * 
     * 패턴:
     * - 클래스명에 Store, Repository, State 포함
     * - StateFlow, BehaviorSubject 등을 property로 가짐
     */
    private fun isStateStore(ktClass: KtClass): Boolean {
        val className = ktClass.name ?: return false
        val hasStateManagement = ktClass.getProperties().any { property ->
            val typeRef = property.typeReference?.text ?: ""
            typeRef.contains("StateFlow") || 
            typeRef.contains("BehaviorSubject") ||
            typeRef.contains("LiveData")
        }
        
        return (className.contains("Store") || 
                className.contains("Repository") ||
                className.contains("State")) && hasStateManagement
    }
    
    /**
     * 파일에서 모든 클래스를 추출합니다.
     */
    private fun extractClasses(ktFile: KtFile): List<KtClass> {
        val classes = mutableListOf<KtClass>()
        ktFile.accept(object : KtTreeVisitorVoid() {
            override fun visitClass(klass: KtClass) {
                super.visitClass(klass)
                classes.add(klass)
            }
        })
        return classes
    }
    
    /**
     * 제네릭 타입 파라미터를 추출합니다.
     * 예: "StateFlow<EditedAlarm?>" -> "EditedAlarm"
     */
    private fun extractGenericType(typeRef: String): String {
        val start = typeRef.indexOf('<')
        val end = typeRef.lastIndexOf('>')
        if (start != -1 && end != -1 && start < end) {
            return typeRef.substring(start + 1, end).replace("?", "").trim()
        }
        return "Unknown"
    }
    
    /**
     * 타입 이름이 data class인지 확인합니다.
     * 
     * @param typeName 확인할 타입 이름 (예: "EditedAlarm", "AlarmValue")
     * @param allFiles 검색할 파일 목록
     * @return data class이면 true, 아니면 false
     */
    private fun checkIfDataClass(typeName: String, allFiles: List<KtFile>): Boolean {
        // 원시 타입이나 표준 라이브러리 타입은 data class가 아님
        val primitiveTypes = setOf(
            "String", "Int", "Long", "Double", "Float", 
            "Boolean", "Char", "Byte", "Short",
            "List", "Map", "Set", "Array",
            "Unknown"
        )
        
        if (typeName in primitiveTypes) {
            return false
        }
        
        // 모든 파일에서 해당 이름의 data class 찾기
        allFiles.forEach { file ->
            var found = false
            file.accept(object : KtTreeVisitorVoid() {
                override fun visitClass(klass: KtClass) {
                    super.visitClass(klass)
                    if (klass.name == typeName && klass.isData()) {
                        found = true
                    }
                }
            })
            if (found) return true
        }
        
        return false
    }
    
    /**
     * State Hub 정보를 JSON 형식으로 출력합니다.
     */
    fun toJson(result: AnalysisResult): String {
        val json = StringBuilder()
        json.append("{\n")

        // ViewModels 섹션
        json.append("  \"viewModels\": [\n")
        result.viewModelInfos.forEachIndexed { index, vm ->
            json.append("    {\n")
            json.append("      \"className\": \"${vm.className}\",\n")
            json.append("      \"filePath\": \"${vm.filePath}\",\n")
            json.append("      \"injectedStores\": [")
            json.append(vm.injectedStores.joinToString(", ") { "\"$it\"" })
            json.append("],\n")
            json.append("      \"ownedStateStreamCount\": ${vm.ownedStateStreams.size}\n")
            json.append("    }")
            if (index < result.viewModelInfos.size - 1) json.append(",")
            json.append("\n")
        }
        json.append("  ],\n")
        
        // State Hubs 섹션
        json.append("  \"stateHubs\": [\n")
        result.stateHubs.forEachIndexed { index, hub ->
            json.append("    {\n")
            json.append("      \"className\": \"${hub.className}\",\n")
            json.append("      \"filePath\": \"${hub.filePath}\",\n")
            json.append("      \"level\": \"${hub.level}\",\n")
            
            if (hub.injectedIntoViewModels.isNotEmpty()) {
                json.append("      \"injectedIntoViewModels\": [")
                json.append(hub.injectedIntoViewModels.joinToString(", ") { "\"$it\"" })
                json.append("],\n")
            }
            
            json.append("      \"stateStreams\": [\n")
            hub.stateStreams.forEachIndexed { sfIndex, sf ->
                json.append("        {\n")
                json.append("          \"propertyName\": \"${sf.propertyName}\",\n")
                json.append("          \"streamType\": \"${sf.streamType}\",\n")
                json.append("          \"dataType\": \"${sf.dataType}\"\n")
                json.append("        }")
                if (sfIndex < hub.stateStreams.size - 1) json.append(",")
                json.append("\n")
            }
            json.append("      ]\n")
            json.append("    }")
            if (index < result.stateHubs.size - 1) json.append(",")
            json.append("\n")
        }
        
        json.append("  ]\n")
        json.append("}\n")
        return json.toString()
    }
}

