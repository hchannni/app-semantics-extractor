import os
import re
import subprocess
from typing import List, Dict, Optional

class AndroidSourceAnalyzer:
    """Android 소스 코드 분석 및 현재 액티비티 매칭을 위한 클래스"""
    
    def __init__(self):
        """
        AndroidSourceAnalyzer 초기화
        
        Args:
            source_code_path: Android 소스 코드 경로 (app-source-code)
        """
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.source_code_path = os.path.join(project_root, "samples", "SimpleAlarmClock", "app-source-code")
        self.adb_path = os.path.expanduser('~/Library/Android/sdk/platform-tools/adb')
        self.activities = []
        self.current_focus = None
        
        # 초기화 시 소스 코드 분석 수행
        self._analyze_source_code()
    
    def _analyze_source_code(self):
        """소스 코드를 분석하여 액티비티 정보를 수집합니다."""
        try:
            self.activities = self._extract_activities_from_source()
            print(f"Found {len(self.activities)} activities in source code.")
        except Exception as e:
            print(f"Error analyzing source code: {e}")
            self.activities = []
    
    def get_current_activity_info(self):
        """
        현재 실행 중인 액티비티 정보와 소스 코드 매칭 결과를 반환합니다.
        
        Returns:
            현재 액티비티 정보가 포함된 딕셔너리
        """
        # 현재 포커스된 액티비티 정보 가져오기
        self.current_focus = self._get_current_focus_activity()
        print(self.current_focus)
    
    def get_current_activity_source_code(self) -> Optional[Dict]:
        """
        현재 Activity의 소스 코드를 불러온다.
        """
        self.current_focus = self._get_current_focus_activity()
        if not self.current_focus:
            print("No current activity found")
            return None
        
        # 딕셔너리 키로 접근
        activity_name = self.current_focus.get('activity_name', '')
        activity_class = self.current_focus.get('activity_class', '')
        if not activity_name:
            print("Activity path not found in current focus")
            return None
        
        # Activity 경로를 실제 파일 경로로 변환
        # com.better.alarm.ui.main.AlarmsListActivity -> com/better/alarm/ui/main/AlarmsListActivity.java
        relative_path = activity_name.replace('.', '/') + '.java'
        file_path = os.path.join(self.source_code_path, "app", "src", "main", "java", relative_path)

        # Kotlin 파일인지도 확인
        # TODO: 애초에 폴더명이 다른 경우도 대응시켜줘야 함 
        if not os.path.exists(file_path):
            kt_file_path = os.path.join(self.source_code_path, "app", "src", "main", "java", relative_path.replace('.java', '.kt'))
            if os.path.exists(kt_file_path):
                file_path = kt_file_path

        # 파일 경로에 파일이 존재하지 않는 경우 (TODO: 에러핸들링)
        if not file_path or not os.path.exists(file_path):
            print(f"Source code not found: {file_path}")
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_content = f.read()
                return {
                    'file_path': file_path,
                    'activity_name': activity_name,
                    'activity_class': self.current_focus['activity_class'],
                    'source_code': source_content,
                    'lines_count': len(source_content.split('\n')),
                    'file_size': len(source_content.encode('utf-8'))
                }
            
        except Exception as e:
            print(f"Error reading source file {file_path}: {e}")
            return None

    def _extract_activities_from_source(self) -> List[Dict]:
        """소스 코드에서 액티비티 정보를 추출합니다."""
        activities = []
        
        # AndroidManifest.xml에서 액티비티 목록 추출
        manifest_activities = self._parse_android_manifest()
        
        # 소스 코드에서 액티비티 클래스 정보 추출
        source_activities = self._parse_activity_source_files()
        
        # 매니페스트와 소스 코드 정보 병합
        for manifest_activity in manifest_activities:
            activity_name = manifest_activity['name']
            
            # 소스 코드에서 해당 액티비티 찾기
            source_info = next((act for act in source_activities 
                              if act['class_name'] in activity_name), {})
            
            activity_info = {
                **manifest_activity,
                **source_info
            }
            activities.append(activity_info)
        
        # 매니페스트에 없지만 소스 코드에 있는 액티비티도 추가
        for source_activity in source_activities:
            class_name = source_activity.get('class_name', '')
            if not any(class_name in act.get('name', '') for act in activities):
                activities.append(source_activity)
        
        return activities
    
    def _parse_android_manifest(self) -> List[Dict]:
        """AndroidManifest.xml에서 액티비티 정보를 파싱합니다."""
        manifest_path = os.path.join(self.source_code_path, "app", "src", "main", "AndroidManifest.xml")
        activities = []
        
        if not os.path.exists(manifest_path):
            print(f"AndroidManifest.xml not found at: {manifest_path}")
            return activities
        
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 패키지명 추출
            package_pattern = r'package="([^"]*)"'
            package_match = re.search(package_pattern, content)
            package_name = package_match.group(1) if package_match else ""
            
            # 액티비티 패턴 매칭 (더 상세한 정보 포함)
            activity_pattern = r'<activity[^>]*android:name="([^"]*)"[^>]*(?:android:label="([^"]*)")?[^>]*>'
            matches = re.findall(activity_pattern, content)
            
            for match in matches:
                activity_name = match[0]
                activity_label = match[1] if len(match) > 1 else ""
                
                # 패키지명 처리
                if activity_name.startswith('.'):
                    full_name = package_name + activity_name
                elif '.' not in activity_name:
                    full_name = f"{package_name}.{activity_name}"
                else:
                    full_name = activity_name
                
                activities.append({
                    'name': full_name,
                    'short_name': activity_name,
                    'label': activity_label,
                    'package': package_name,
                    'type': 'activity',
                    'source': 'manifest'
                })
        
        except Exception as e:
            print(f"Error parsing AndroidManifest.xml: {e}")
        
        return activities
    
    def _parse_activity_source_files(self) -> List[Dict]:
        """소스 코드에서 액티비티 클래스 정보를 추출합니다."""
        activities = []
        
        # Java/Kotlin 소스 디렉토리 찾기
        possible_paths = [
            os.path.join(self.source_code_path, "app", "src", "main", "java"),
            os.path.join(self.source_code_path, "app", "src", "main", "kotlin")
        ]
        
        source_dir = None
        for path in possible_paths:
            if os.path.exists(path):
                source_dir = path
                break
        
        if not source_dir:
            print("Source directory not found")
            return activities
        
        # 모든 .java, .kt 파일 검색
        for root, dirs, files in os.walk(source_dir):
            for file in files:
                if file.endswith(('.java', '.kt')):
                    file_path = os.path.join(root, file)
                    activity_info = self._parse_single_activity_file(file_path)
                    if activity_info:
                        activities.append(activity_info)
        
        return activities
    
    def _parse_single_activity_file(self, file_path: str) -> Optional[Dict]:
        """단일 액티비티 파일을 파싱합니다."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 액티비티 클래스인지 확인 (더 정확한 패턴)
            activity_patterns = [
                r'class\s+(\w+).*?extends\s+(\w*Activity\w*)',  # Java
                r'class\s+(\w+).*?:\s*(\w*Activity\w*)',        # Kotlin
                r'class\s+(\w+).*?extends\s+AppCompatActivity',  # AppCompatActivity
                r'class\s+(\w+).*?:\s*AppCompatActivity'         # Kotlin AppCompatActivity
            ]
            
            class_name = None
            parent_class = None
            
            for pattern in activity_patterns:
                match = re.search(pattern, content, re.DOTALL)
                if match:
                    class_name = match.group(1)
                    parent_class = match.group(2) if len(match.groups()) > 1 else "Activity"
                    break
            
            if not class_name:
                return None
            
            # 패키지명 추출
            package_pattern = r'package\s+([^;\s]+)'
            package_match = re.search(package_pattern, content)
            package_name = package_match.group(1) if package_match else ""
            
            # 메서드 정보 추출
            methods = self._extract_activity_methods(content)
            
            # 임포트 정보 추출
            imports = self._extract_imports(content)
            
            return {
                'class_name': class_name,
                'package_name': package_name,
                'full_class_name': f"{package_name}.{class_name}" if package_name else class_name,
                'parent_class': parent_class,
                'file_path': file_path,
                'methods': methods,
                'imports': imports,
                'type': 'activity',
                'source': 'source_code'
            }
        
        except Exception as e:
            print(f"Error parsing activity file {file_path}: {e}")
            return None
    
    def _extract_activity_methods(self, content: str) -> List[Dict]:
        """액티비티 메서드들을 추출합니다."""
        lifecycle_methods = [
            'onCreate', 'onStart', 'onResume', 'onPause', 
            'onStop', 'onDestroy', 'onRestart', 'onNewIntent',
            'onSaveInstanceState', 'onRestoreInstanceState'
        ]
        
        found_methods = []
        
        for method in lifecycle_methods:
            # Java와 Kotlin 패턴 모두 지원
            patterns = [
                rf'@Override\s+(?:protected\s+|public\s+)?void\s+{method}\s*\([^)]*\)',  # Java
                rf'override\s+fun\s+{method}\s*\([^)]*\)',  # Kotlin
                rf'(?:protected\s+|public\s+)?void\s+{method}\s*\([^)]*\)'  # Java without @Override
            ]
            
            for pattern in patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    found_methods.append({
                        'name': method,
                        'type': 'lifecycle'
                    })
                    break
        
        # 기타 중요 메서드들도 찾기
        other_methods = ['onBackPressed', 'onOptionsItemSelected', 'onActivityResult']
        for method in other_methods:
            patterns = [
                rf'@Override\s+(?:public\s+)?(?:void\s+|boolean\s+){method}\s*\([^)]*\)',
                rf'override\s+fun\s+{method}\s*\([^)]*\)',
                rf'(?:public\s+)?(?:void\s+|boolean\s+){method}\s*\([^)]*\)'
            ]
            
            for pattern in patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    found_methods.append({
                        'name': method,
                        'type': 'callback'
                    })
                    break
        
        return found_methods
    
    def _extract_imports(self, content: str) -> List[str]:
        """임포트 구문을 추출합니다."""
        import_pattern = r'import\s+([^;\s]+)'
        matches = re.findall(import_pattern, content)
        return matches
    
    def _get_current_focus_activity(self) -> Optional[Dict]:
        """ADB를 통해 현재 포커스된 액티비티를 가져옵니다."""
        try:
            result = subprocess.check_output(
                f"{self.adb_path} shell dumpsys window | grep mCurrentFocus",
                shell=True
            ).decode().strip()
            
            print(f"Current Focus: {result}")
            
            # 더 정확한 패턴: mCurrentFocus=Window{hash u0 package/full.activity.name}
            focus_pattern = r'mCurrentFocus=Window\{[^}]+\s+([a-zA-Z0-9_.]+)/([a-zA-Z0-9_.]+)\}'
            match = re.search(focus_pattern, result)
            
            if match:
                package_name = match.group(1)  # com.better.alarm.debug
                full_activity_name = match.group(2)  # com.better.alarm.ui.main.AlarmsListActivity
                
                # 액티비티 클래스명만 추출 (마지막 점 이후)
                activity_class_name = full_activity_name.split('.')[-1]  # AlarmsListActivity
                
                return {
                    'package': package_name,
                    'activity_name': full_activity_name,
                    'activity_class': activity_class_name,
                    'full_name': f"{package_name}/{full_activity_name}",
                    'raw_output': result
                }
            
        except subprocess.CalledProcessError as e:
            print(f"Error getting current activity: {e}")
            return None

    def get_all_activities(self) -> List[Dict]:
        """모든 액티비티 정보를 반환합니다."""
        return self.activities
    
    def find_activity_by_name(self, name: str) -> Optional[Dict]:
        """이름으로 액티비티를 찾습니다."""
        for activity in self.activities:
            if (activity.get('name') == name or 
                activity.get('class_name') == name or 
                activity.get('full_class_name') == name):
                return activity
        return None
    
    
# 사용 예시
if __name__ == "__main__": 
    # 분석기 생성
    analyzer = AndroidSourceAnalyzer()
    
    # 현재 액티비티 정보만 가져오기
    current_info = analyzer.get_current_activity_info()
    
    # 특정 액티비티 찾기
    main_activity = analyzer.find_activity_by_name("MainActivity")
    if main_activity:
        print(f"\nFound MainActivity: {main_activity}")