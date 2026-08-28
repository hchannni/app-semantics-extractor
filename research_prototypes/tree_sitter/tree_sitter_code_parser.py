import os
import tree_sitter_kotlin as tskotlin
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Query, QueryCursor
import json
from datetime import datetime


class TreeSitterCodeParser:
    """
    Tree-sitter를 사용하여 Android 프로젝트의 Kotlin/Java 소스코드를 파싱하는 클래스
    - Kotlin 우선적으로 구현
    - (TODO) Java 파서 지원 필요
    """

    def __init__(self):
        self.kotlin_language = Language(tskotlin.language())
        self.java_language = Language(tsjava.language())
        
        self.kotlin_parser = Parser(self.kotlin_language)
        self.java_parser = Parser(self.java_language)

        # tree-sitter queries
        self.kt_query_r_id = Query(self.kotlin_language, """
        (navigation_expression
            (navigation_expression
                (identifier) @R (#eq? @R "R")
                (identifier) @id (#eq? @id "id")
            )
            (identifier) @element_id
        ) @find_call_with_r_id
        """)
        
        self.java_query_r_id = None # (TODO)

        self.kt_query_findviewbyid_direct_call = Query(self.kotlin_language, """
        (call_expression
            (navigation_expression
                (identifier) ;
                (identifier) @func_name (#eq? @func_name "findViewById")
            )
            (value_arguments
                (value_argument
                    (navigation_expression
                        (navigation_expression (identifier) @R (#eq? @R "R"))
                    )
                )
            )
        )
        """)
        
        self.java_query_find_view = None # (TODO)

        self.kt_query_find_view_wrapper_call = Query(self.kotlin_language, """
        (call_expression
            (navigation_expression
                (identifier) ;
                (identifier) @func_name (#eq? @func_name "findViewById")
            )
            (value_arguments
                (value_argument
                    (identifier) @arg_variable
                )
            )
        )
        """)

        self.kt_query_function_definition = Query(self.kotlin_language, """
        (function_declaration
            (identifier) @func_name
            (function_body) @func_body
        )
        """)

        self.kt_query_all_function_calls = Query(self.kotlin_language, """
        [
            (call_expression
                (identifier) @func_name
            ) @call_site
            
            (call_expression
                (navigation_expression
                    (identifier) ;
                    (identifier) @func_name
                )
            ) @call_site
        ]
        """)

        self.kt_query_assignment = Query(self.kotlin_language, """
        (assignment
            (navigation_expression) @ui_sink
            (navigation_expression) @data_source
        )
        """)

        self.kt_query_class_definition = Query(self.kotlin_language, """
        (class_declaration
            (identifier) @class_name
            (class_body)? @class_body 
        )
        """)

        self.kt_query_property_declaration = Query(self.kotlin_language, """
        (property_declaration
            (variable_declaration
                (identifier) @prop_name
                (_)? @prop_type
            )
            (_)? @init_expr
        )
        """)

        self.kt_query_override_function = Query(self.kotlin_language, """
        (function_declaration
            (modifiers) @modifiers
            (identifier) @func_name
            (function_value_parameters) @params
            (function_body) @func_body
        )
        """)

        self.kt_query_variable_declaration = Query(self.kotlin_language, """
        (property_declaration
            (variable_declaration (identifier) @var_name)
            (_) @init_expr
        )                                            
        """)

        self.kt_query_all_navigation_expressions = Query(self.kotlin_language, """
        (navigation_expression) @nav_expr
        """)

    def debug_ast_structure(self, file_path: str, language: str):
        """
        코드의 AST 구조를 출력하여 실제 노드 타입을 확인합니다.
        각 노드의 위치 정보(행, 열)와 해당하는 정확한 코드 부분을 함께 표시합니다.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()
            
            source_lines = source_code.split('\n')

            if language == 'kotlin':
                tree = self.kotlin_parser.parse(bytes(source_code, 'utf8'))
            elif language == 'java':
                tree = self.java_parser.parse(bytes(source_code, 'utf8'))
            else:
                return
            
            def get_node_text(node):
                """노드의 정확한 텍스트를 추출합니다."""
                start_row, start_col = node.start_point
                end_row, end_col = node.end_point
                
                if start_row == end_row:
                    # 한 줄에 있는 경우
                    if start_row < len(source_lines):
                        line = source_lines[start_row]
                        return line[start_col:end_col]
                else:
                    # 여러 줄에 걸쳐 있는 경우
                    result = []
                    for row in range(start_row, end_row + 1):
                        if row < len(source_lines):
                            line = source_lines[row]
                            if row == start_row:
                                result.append(line[start_col:])
                            elif row == end_row:
                                result.append(line[:end_col])
                            else:
                                result.append(line)
                    return '\n'.join(result)
                return ""
            
            def print_node(node, indent=0):
                # 노드의 시작과 끝 위치 정보
                start_row, start_col = node.start_point
                end_row, end_col = node.end_point
                
                # 위치 정보 포맷팅 (1-based로 표시)
                position = f"[{start_row + 1}, {start_col}] - [{end_row + 1}, {end_col}]"
                
                # 노드의 정확한 텍스트 추출
                node_text = get_node_text(node)
                
                # 텍스트가 너무 길면 줄바꿈 문자를 제거하고 줄여서 표시
                display_text = node_text.replace('\n', '\\n').replace('\r', '\\r')
                if len(display_text) > 100:
                    display_text = display_text[:97] + "..."
                
                # 노드 정보 출력
                print("  " * indent + f"{node.type} {position}")
                if display_text.strip():  # 빈 텍스트가 아닌 경우에만 출력
                    print("  " * indent + f"  → \"{display_text}\"")
                
                # 자식 노드들 재귀 출력
                for child in node.children:
                    print_node(child, indent + 1)
            
            print(f"\n--- {file_path} ({language.upper()}) AST Structure ---")
            print_node(tree.root_node)
            
        except FileNotFoundError:
            print(f"파일을 찾을 수 없습니다: {file_path}")
        except Exception as e:
            print(f"파일 파싱 중 오류 발생: {file_path} - {str(e)}")

    def get_source_files(self, root_dir: str) -> list:
        source_files = []
        java_dir = os.path.join(root_dir, "app", "src", "main", "java")
        
        if os.path.exists(java_dir):
            for root, dirs, files in os.walk(java_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if file.endswith('.kt'):
                        source_files.append((file_path, 'kotlin'))
                    elif file.endswith('.java'):
                        source_files.append((file_path, 'java'))
        
        return source_files

    def parse_file(self, file_path: str, language: str) -> dict:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()
            
            if language == 'kotlin':
                tree = self.kotlin_parser.parse(bytes(source_code, 'utf8'))
            elif language == 'java':
                tree = self.java_parser.parse(bytes(source_code, 'utf8'))
            
            return {
                'file_path': file_path,
                'language': language,
                'tree': tree,
                'source_code': source_code,
                'success': True
            }
            
        except Exception as e:
            return {
                'file_path': file_path,
                'language': language,
                'error': str(e),
                'success': False
            }
    
    def find_ui_elements(self, parsed_file_data: dict) -> list: 
        if not parsed_file_data['success']:
            print(f"file data - 'success' is false")
            return []
        
        tree = parsed_file_data['tree']
        language = parsed_file_data['language']

        if language == 'kotlin': query = self.kt_query_r_id
        elif language == 'java': query = self.java_query_r_id
        else: return []
        
        if query is None:
            print('query is None')
            return []
        
        cursor = QueryCursor(query)
        captures = cursor.captures(tree.root_node)
        
        print(f"DEBUG: captures type: {type(captures)}")
        print(f"DEBUG: captures content: {captures}")
        
        findings = []

        # captures는 dict 형태: {'capture_name': [Node, Node, ...]}
        for capture_name, nodes in captures.items():
            print(f"DEBUG: capture_name: {capture_name}, nodes count: {len(nodes)}")
            
            if capture_name == 'find_call_with_r_id':
                for node in nodes:
                    print(f"DEBUG: processing node: {node.type}, text: {node.text.decode('utf8')}")
                    line_number = node.start_point[0] + 1
                    findings.append({
                        'text': node.text.decode('utf8'),
                        'line': line_number,
                        'node': node
                    })
        
        print(f"DEBUG: findings count: {len(findings)}")
        return findings
    
    def find_findviewbyid_direct_calls(self, parsed_file_data: dict, root_node=None) -> list:
        if not parsed_file_data['success']:
            return []
            
        tree = parsed_file_data['tree']
        language = parsed_file_data['language']

        # 검색 대상 노드를 지정하지 않으면 파일 전체 트리 사용
        if root_node is None:
            root_node = tree.root_node
        
        if language == 'kotlin': query = self.kt_query_findviewbyid_direct_call
        elif language == 'java': query = self.java_query_find_view
        else: return []
        
        if query is None: return []
        
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        findings = []
    
        found_call_node_ids = set()
        
        for capture_name, nodes in captures.items():
            if capture_name == 'func_name':
                for node in nodes:
                    if language == 'kotlin':
                        call_node = node.parent.parent # Kotlin: (identifier) -> (navigation_expression) -> (call_expression)
                    elif language == 'java':
                        pass
                    
                    if call_node.id not in found_call_node_ids:
                        line_number = call_node.start_point[0] + 1 # 1-based
                        findings.append({
                            'text': call_node.text.decode('utf8'),
                            'line': line_number,
                            'node': call_node
                        })
                        found_call_node_ids.add(call_node.id)
                        
        return findings

    def find_wrapper_functions_for_findviewbyid(self, parsed_file_data: dict) -> list:
        if not parsed_file_data['success']:
            print(f"'find_wrapper_function': parsing FAILED data")
            return []
        
        tree = parsed_file_data['tree']
        language = parsed_file_data['language']

        if language == 'kotlin': 
            query_func_def = self.kt_query_function_definition
            query_wrapper_call = self.kt_query_find_view_wrapper_call
        elif language == 'java': return [] # (TODO: java 버전 tree-sitter query 구현)
        else: return []
        
        if query_func_def is None or query_wrapper_call is None:
            print('query is None')
            return []
        
        cursor = QueryCursor(query_func_def)
        captures = cursor.captures(tree.root_node)
        findings = []

        functions_found = {} # (func_name, body_node) 쌍을 저장할 딕셔너리

        if 'func_name' in captures:
            for name_node in captures['func_name']:
                func_decl_id = name_node.parent.id
                if func_decl_id not in functions_found:
                    functions_found[func_decl_id] = {}
                functions_found[func_decl_id]['name_node'] = name_node
        if 'func_body' in captures:
            for body_node in captures['func_body']:
                func_decl_id = body_node.parent.id
                if func_decl_id not in functions_found:
                    functions_found[func_decl_id] = {}
                functions_found[func_decl_id]['body_node'] = body_node

        for func_data in functions_found.values():
            if 'name_node' not in func_data or 'body_node' not in func_data:
                continue

            name_node = func_data['name_node']
            body_node = func_data['body_node']
            
            check_wrapper_cursor = QueryCursor(query_wrapper_call)
            internal_captures = check_wrapper_cursor.captures(body_node) # 이 함수의 '본문(body_node)' 안에서 'findViewById(variable)' 호출을 검색
            if internal_captures and 'func_name' in internal_captures:
                line_number = name_node.start_point[0] + 1
                
                internal_calls_info = [] # (내부 호출 정보도 추가)
                for call_name_node in internal_captures['func_name']:
                    call_node = call_name_node.parent.parent
                    internal_calls_info.append({
                        "text": call_node.text.decode('utf8'),
                        "line": call_node.start_point[0] + 1
                    })

                findings.append({
                    'wrapper_name': name_node.text.decode('utf8'),
                    'line': line_number,
                    'node': name_node.parent, # function_declaration 노드
                    'internal_calls': internal_calls_info
                })
        
        return findings

    def find_indirect_wrapper_function_calls(self, parsed_file_data: dict, wrapper_names_set: set) -> list:
        if not parsed_file_data.get('success'):
            return []

        tree = parsed_file_data['tree']
        query = self.kt_query_all_function_calls
        if query is None: return []

        cursor = QueryCursor(query)
        captures = cursor.captures(tree.root_node)
        findings = []

        if 'func_name' in captures:
            for name_node in captures['func_name']:
                func_name = name_node.text.decode('utf8')
                
                if func_name in wrapper_names_set: # findViewById() 함수의 래퍼 함수인지 확인
                    call_site_node = name_node
                    while call_site_node.parent and call_site_node.type != 'call_expression':
                        call_site_node = call_site_node.parent # 함수명 노드에서 부모 노드로 올라가면서 'call_expression' 노드를 찾는다
                    
                    if call_site_node.type == 'call_expression':
                        line_number = call_site_node.start_point[0] + 1
                        findings.append({
                            'text': call_site_node.text.decode('utf8'),
                            'wrapper_name': func_name,
                            'line': line_number,
                            'node': call_site_node
                        })

        return findings

    def find_uisink_datasource_assignments(self, parsed_file_data: dict) -> list:
        if not parsed_file_data.get('success'):
            print("find_assignments: parsing FAILED data")
            return []
        
        tree = parsed_file_data['tree']
        query = self.kt_query_assignment
        if query is None: 
            print("find_assignments: query is None")
            return []

        cursor = QueryCursor(query)
        captures = cursor.captures(tree.root_node)
        findings = []

        assignments_found = {} # (ui_sink, data_source) 쌍을 저장할 딕셔너리 (key: 할당 노드 ID)

        if 'ui_sink' in captures:
            print(f"DEBUG: ui_sink captures found: {len(captures['ui_sink'])}")
            for node in captures['ui_sink']:
                print(f"DEBUG: ui_sink node: {node.type}, text: {node.text.decode('utf8')}")
                assignment_id = node.parent.id
                if assignment_id not in assignments_found:
                    assignments_found[assignment_id] = {}
                assignments_found[assignment_id]['ui_sink_node'] = node
        if 'data_source' in captures:
            print(f"DEBUG: data_source captures found: {len(captures['data_source'])}")
            for node in captures['data_source']:
                print(f"DEBUG: data_source node: {node.type}, text: {node.text.decode('utf8')}")
                assignment_id = node.parent.id
                if assignment_id not in assignments_found:
                    assignments_found[assignment_id] = {}
                assignments_found[assignment_id]['data_source_node'] = node

        for assignment_id, data in assignments_found.items():
            if 'ui_sink_node' in data and 'data_source_node' in data:
                sink_node = data['ui_sink_node']
                source_node = data['data_source_node']

                line_number = sink_node.parent.start_point[0] + 1 # 할당 구문의 line number
                findings.append({
                    'ui_sink': sink_node.text.decode('utf8'),
                    'data_source': source_node.text.decode('utf8'),
                    'line': line_number,
                    'node': sink_node.parent # assignment 노드
                })
        
        print(f"DEBUG: final findings count: {len(findings)}")
        return findings

    def find_view_holders(self, parsed_file_data: dict) -> list:
        if not parsed_file_data['success']:
            print(f"'find_view_holders' parsing FAILED data")
            return []
        
        tree = parsed_file_data['tree']
        class_query = self.kt_query_class_definition
        prop_query = self.kt_query_property_declaration

        if class_query is None or prop_query is None: return []

        class_query_cursor = QueryCursor(class_query)
        class_captures = class_query_cursor.captures(tree.root_node)
        findings = []

        classes_found = {} # 클래스 정의 노드 (이름, 본문) 저장할 딕셔너리
        if 'class_name' in class_captures:
            for name_node in class_captures['class_name']:
                class_decl_id = name_node.parent.id
                if class_decl_id not in classes_found: classes_found[class_decl_id] = {}
                classes_found[class_decl_id]['name_node'] = name_node
        if 'class_body' in class_captures:
            for body_node in class_captures['class_body']:
                class_decl_id = body_node.parent.id
                if class_decl_id not in classes_found: classes_found[class_decl_id] = {}
                classes_found[class_decl_id]['body_node'] = body_node

        for class_data in classes_found.values():
            if 'name_node' not in class_data or 'body_node' not in class_data:
                continue 

            class_name_node = class_data['name_node']
            class_body_node = class_data['body_node']
            class_name = class_name_node.text.decode('utf8')

            # --- 휴리스틱: 클래스 이름에 "Holder" 포함 ---
            if "Holder" not in class_name:
                continue

            print(f"  -> Potential ViewHolder found: {class_name}")
            
            holder_properties = []
            prop_query_cursor = QueryCursor(prop_query)
            prop_captures = prop_query_cursor.captures(class_body_node)

            props_temp = {} # 속성 이름, 타입, 초기화 정보를 임시 저장할 딕셔너리
            if 'prop_name' in prop_captures:
                for node in prop_captures['prop_name']:
                    prop_decl_id = node.parent.parent.id # name -> var_decl -> prop_decl
                    if prop_decl_id not in props_temp: props_temp[prop_decl_id] = {}
                    props_temp[prop_decl_id]['name'] = node.text.decode('utf8')
            if 'prop_type' in prop_captures:
                for node in prop_captures['prop_type']:
                    prop_decl_id = node.parent.parent.id
                    if prop_decl_id not in props_temp: props_temp[prop_decl_id] = {}
                    props_temp[prop_decl_id]['type'] = node.text.decode('utf8') if node else None
            if 'init_expr' in prop_captures:
                for node in prop_captures['init_expr']:
                    prop_decl_id = node.parent.id # init -> prop_decl
                    if prop_decl_id not in props_temp: props_temp[prop_decl_id] = {}
                    props_temp[prop_decl_id]['init'] = node.text.decode('utf8') if node else None
                    props_temp[prop_decl_id]['init_node'] = node # 초기화 분석을 위해 노드 저장

            android_widget_types = {"TextView", "ImageView", "CompoundButton", "View", "DigitalClock"} # (TODO) widget types 업데이트
            
            for prop_data in props_temp.values():
                prop_name = prop_data.get('name')
                prop_type = prop_data.get('type')
                prop_init = prop_data.get('init')
                init_node = prop_data.get('init_node')
                is_ui_widget = False

                if prop_type and prop_type in android_widget_types:
                    is_ui_widget = True
                
                init_analysis = None
                if init_node:
                    init_query_cursor = QueryCursor(self.kt_query_all_function_calls)
                    init_captures = init_query_cursor.captures(init_node)
                    if 'func_name' in init_captures:
                        for name_node in init_captures['func_name']:
                            func_name = name_node.text.decode('utf8')
                            if func_name == 'find' or func_name == 'findViewById':
                                is_ui_widget = True
                                # 어떤 ID를 사용하는지 분석
                                call_site_node = name_node
                                while call_site_node.parent and call_site_node.type != 'call_expression':
                                    call_site_node = call_site_node.parent
                                if call_site_node.type == 'call_expression':
                                    # 함수 호출의 인수 부분에서 R.id.xxx 추출
                                    call_text = call_site_node.text.decode('utf8')
                                    print(f"    호출 구문 전체: {call_text}")
                                    
                                    # R.id.xxx 패턴을 정규식으로 추출
                                    import re
                                    r_id_pattern = r'R\.id\.(\w+)'
                                    match = re.search(r_id_pattern, call_text)
                                    if match:
                                        r_id_name = match.group(1)  # R.id. 제거된 순수 이름
                                        init_analysis = {
                                            "init_call": func_name, 
                                            "arg": f"R.id.{r_id_name}",
                                            "r_id_extracted": r_id_name
                                        }
                                        print(f"    초기화 분석: {prop_name} = {func_name}(R.id.{r_id_name})")
                                    else:
                                        print(f"    R.id 패턴을 찾을 수 없음: {call_text}")
                                break
                
                if prop_name and is_ui_widget:
                    holder_properties.append({
                        "name": prop_name,
                        "type": prop_type,
                        "init": prop_init,
                        "init_analysis": init_analysis
                    })

            if holder_properties:
                findings.append({
                    "class_name": class_name,
                    "properties": holder_properties,
                    "node": class_name_node.parent # class_declaration 노드
                })
        
        return findings

    def find_view_rendering_methods(self, parsed_file_data: dict, view_holder_names: set) -> list:
        if not parsed_file_data.get('success'): 
            print(f"'find_view_rendering_methods' parsing FAILED data")
            return []

        tree = parsed_file_data['tree']
        query = self.kt_query_override_function
        if query is None: return []

        cursor = QueryCursor(query)
        captures = cursor.captures(tree.root_node)
        findings = []

        override_funcs = {}
        if 'func_name' in captures:
            for node in captures['func_name']:
                func_decl_id = node.parent.id
                if func_decl_id not in override_funcs: override_funcs[func_decl_id] = {}
                override_funcs[func_decl_id]['name_node'] = node
        if 'params' in captures:
            for node in captures['params']:
                func_decl_id = node.parent.id
                if func_decl_id not in override_funcs: override_funcs[func_decl_id] = {}
                override_funcs[func_decl_id]['params_node'] = node
        if 'func_body' in captures:
            for node in captures['func_body']:
                func_decl_id = node.parent.id
                if func_decl_id not in override_funcs: override_funcs[func_decl_id] = {}
                override_funcs[func_decl_id]['body_node'] = node
        if 'modifiers' in captures:
            for node in captures['modifiers']:
                func_decl_id = node.parent.id
                if func_decl_id not in override_funcs: override_funcs[func_decl_id] = {}
                override_funcs[func_decl_id]['modifiers_node'] = node
        
        for func_data in override_funcs.values():
            if 'name_node' not in func_data or 'params_node' not in func_data or 'body_node' not in func_data:
                continue

            name_node = func_data['name_node']
            params_node = func_data['params_node']
            body_node = func_data['body_node']
            modifiers_node = func_data.get('modifiers_node')
            method_name = name_node.text.decode('utf8')

            # override 키워드 확인
            is_override = False
            if modifiers_node:
                modifiers_text = modifiers_node.text.decode('utf8')
                if 'override' in modifiers_text:
                    is_override = True
            
            # override가 아니면서 getView도 아니면 스킵 (일반 함수도 포함하도록 수정)
            if not is_override and method_name not in {"getView", "onBindViewHolder"}:
                continue

            holder_param_name = None
            data_param_name = None

            for param_node in params_node.named_children:
                if params_node.type == 'parameter':
                    param_name_node = None
                    param_type_node = None

                    if param_node.named_child_count > 0:
                        param_name_node = param_node.named_children[0] # 파라미터의 첫 번째 자식은 이름(identifier)
                    if param_node.named_child_count > 1:
                        param_type_node = param_node.named_children[1] # 파라미터의 두 번째 자식은 타입(type)일 수 있음

                    if param_name_node:
                        param_name = param_name_node.text.decode('utf8')
                        param_type = param_type_node.text.decode('utf8') if param_type_node else None

                        # (TODO) 뷰 렌더링 함수 파라미터에서 어떤 값을 찾아낼 것인가? 발전시킬 여지
                        # if "holder" in param_name.lower() or (param_type and param_type in view_holder_names):
                        #     holder_param_name = param_name
                        #     print(f"    - ViewHolder parameter identified: {param_name} (Type: {param_type})")
                        # elif param_name in {"item", "data", "alarm", "value"} or (param_type and "Value" in param_type): # 예시
                        #     data_param_name = param_name
                        #     print(f"    - Data parameter identified: {param_name} (Type: {param_type})")

            if holder_param_name or data_param_name: # 하나라도 식별되면 결과에 추가
                findings.append({
                    "method_name": method_name,
                    "holder_param": holder_param_name,
                    "data_param": data_param_name,
                    "body_node": body_node,
                    "node": name_node.parent # function_declaration 노드
                })
            else: # 파라미터에서 못 찾았으면 일단 본문만이라도 저장 (Phase 3에서 분석)
                findings.append({
                    "method_name": method_name, 
                    "holder_param": None, 
                    "data_param": None, 
                    "body_node": body_node, 
                    "node": name_node.parent
                })
        
        return findings
    
    def analyze_view_and_data_interactions_in_method(self, parsed_file_data: dict, method_info: dict, view_holder_properties_map: dict, data_schema_properties: set) -> list:
        """
        [Phase 3] 렌더링 메서드 본문 내에서 뷰 홀더와 데이터 객체 간의 상호작용(할당, 함수 호출)을 분석합니다.
        
        Args:
            parsed_file_data (dict): 현재 파일의 파싱 데이터
            method_info (dict): Phase 2에서 찾은 뷰 렌더링 메서드 정보
            view_holder_properties_map (dict): { "ClassName": {"prop1", "prop2"} } 형태의 전역 뷰 홀더 속성 맵
            data_schema_properties (set): {"prop1", "prop2"} 형태의 데이터 스키마 속성 셋
        """
        if not parsed_file_data['success'] or 'body_node' not in method_info:
            return []
        
        body_node = method_info['body_node']
        holder_var = method_info['holder_param'] 
        data_var = method_info['data_param']

        interactions = []
        
        # --- 1. 앵커 변수 식별 (Anchor Variable Identification) ---
        # holder_var나 data_var가 None일 경우 (e.g., getView), 본문 내 사용량으로 역추적
        if holder_var is None or data_var is None:
            nav_query = self.kt_query_all_navigation_expressions
            if nav_query is None: 
                return []
            
            cursor = QueryCursor(nav_query)
            nav_captures = cursor.captures(body_node)
            holder_var_candidates = {}
            data_var_candidates = {}

            if 'nav_expr' in nav_captures:
                for node in nav_captures['nav_expr']:
                    if node.named_child_count < 2: continue # a.b 형태가 아님
                    
                    base_node = node.named_children[0]
                    prop_node = node.named_children[-1] # 마지막 자식이 속성 이름
                    
                    # 기반 변수가 단순 이름(identifier)인 경우만 처리
                    if base_node.type == 'identifier':
                        base_name = base_node.text.decode('utf8')
                        prop_name = prop_node.text.decode('utf8')

                        # 1a. 이 속성이 뷰 홀더 속성 목록에 있는가?
                        for holder_props in view_holder_properties_map.values():
                            if prop_name in holder_props:
                                holder_var_candidates[base_name] = holder_var_candidates.get(base_name, 0) + 1
                                break # 여러 홀더에 중복 속성이 있어도 한번만 카운트
                                
                        # 1b. 이 속성이 데이터 스키마 속성 목록에 있는가?
                        if prop_name in data_schema_properties:
                            data_var_candidates[base_name] = data_var_candidates.get(base_name, 0) + 1

            # 가장 많이 사용된 변수를 홀더/데이터 변수로 확정 (휴리스틱)
            if holder_var is None and holder_var_candidates:
                holder_var = max(holder_var_candidates, key=holder_var_candidates.get)
                print(f"    - ViewHolder variable Inferred by usage: '{holder_var}' (found {holder_var_candidates[holder_var]} usages)")
            
            if data_var is None and data_var_candidates:
                data_var = max(data_var_candidates, key=data_var_candidates.get)
                print(f"    - Data variable Inferred by usage: '{data_var}' (found {data_var_candidates[data_var]} usages)")

        # --- 2. 상호작용 탐색 (Interaction Hunting) ---
        if not holder_var or not data_var:
            print("    - ViewHolder or Data variable could not be reliably identified. Skipping detailed interaction analysis.")
            return []

        # 2a. 할당 구문 분석
        assignment_query = self.kt_query_assignment
        if assignment_query:
            cursor = QueryCursor(assignment_query)
            assignment_captures = cursor.captures(body_node)
            assignments_found = {}
            # (find_assignments와 동일한 로직으로 캡처 매핑)
            if 'ui_sink' in assignment_captures:
                for node in assignment_captures['ui_sink']:
                    assignment_id = node.parent.id
                    if assignment_id not in assignments_found: assignments_found[assignment_id] = {}
                    assignments_found[assignment_id]['ui_sink_node'] = node
            if 'data_source' in assignment_captures:
                for node in assignment_captures['data_source']:
                    assignment_id = node.parent.id
                    if assignment_id not in assignments_found: assignments_found[assignment_id] = {}
                    assignments_found[assignment_id]['data_source_node'] = node
            
            for data in assignments_found.values():
                if 'ui_sink_node' in data and 'data_source_node' in data:
                    sink_node = data['ui_sink_node']
                    source_node = data['data_source_node']
                    sink_text = sink_node.text.decode('utf8')
                    source_text = source_node.text.decode('utf8')

                    # sink가 'holder_var.'로 시작하고 source가 'data_var.'로 시작하는가?
                    if sink_text.startswith(holder_var + '.') and source_text.startswith(data_var + '.'):
                        line = sink_node.parent.start_point[0] + 1
                        interactions.append({
                            'type': 'assignment_direct',
                            'sink': sink_text,
                            'source': source_text,
                            'line': line, 'node': sink_node.parent
                        })
                    
                    # sink가 'holder_var.'로 시작하고 source가 'data_var'를 포함하는 함수 호출인가?
                    elif sink_text.startswith(holder_var + '.') and data_var in source_text and source_node.parent.type == 'assignment':
                        line = sink_node.parent.start_point[0] + 1
                        interactions.append({
                            'type': 'assignment_indirect_call',
                            'sink': sink_text,
                            'source_call': source_text,
                            'line': line, 'node': sink_node.parent
                        })

        # 2b. 함수 호출 분석
        call_query = self.kt_query_all_function_calls
        if call_query:
            cursor = QueryCursor(call_query)
            call_captures = cursor.captures(body_node)
            if 'call_site' in call_captures:
                for call_node in call_captures['call_site']:
                    call_text = call_node.text.decode('utf8')
                    
                    # 함수 호출 텍스트에 holder_var와 data_var가 모두 포함되는가?
                    if holder_var in call_text and data_var in call_text:
                        line = call_node.start_point[0] + 1
                        interactions.append({
                            'type': 'call_interaction',
                            'full_text': call_text,
                            'line': line,
                            'node': call_node
                        })
                    
                    # (심화) 데이터 흐름 추적 (digitalClock 예시)
                    elif holder_var in call_text and data_var not in call_text:
                        # 1. 'data_var'에 의해 오염된 변수(e.g., 'c')를 찾음 (간략화)
                        # 2. 그 'c'가 이 호출('call_text')에 사용되었는지 확인
                        # (이 부분은 'c'를 식별하는 로직이 추가로 필요하며 현재는 생략)
                        pass 
                        
        return interactions


if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    app_source_code_dir = os.path.join(project_root, "samples", "SimpleAlarmClock", "app-source-code")
    
    parser = TreeSitterCodeParser()
    
    source_files = parser.get_source_files(app_source_code_dir)
    print(f"--- 총 {len(source_files)}개의 소스 파일 검색됨 ---")

    # JSON 파일에서 데이터 스키마 정보 로드
    schema_file_path = os.path.join(project_root, "samples", "SimpleAlarmClock", "data_model_schema_20251015_184855.json")
    alarm_value_properties = set()
    
    try:
        with open(schema_file_path, 'r', encoding='utf-8') as f:
            schema_data = json.load(f)
        
        print(f"--- 데이터 스키마 로드 완료 ---")
        print(f"총 {schema_data['metadata']['total_classes']}개의 데이터 클래스 발견")
        
        # AlarmValues 스키마에서 속성명들 추출
        for schema in schema_data['schemas']:
            if schema['name'] == 'AlarmValues':
                for prop in schema['properties']:
                    alarm_value_properties.add(prop['name'])
                print(f"AlarmValues 속성들: {sorted(alarm_value_properties)}")
                break
        
        if not alarm_value_properties:
            print("⚠️ AlarmValues 스키마를 찾을 수 없습니다. 기본값을 사용합니다.")
            alarm_value_properties = {"state", "id", "isEnabled", "hour", "minutes", "label", "daysOfWeek"}
            
    except FileNotFoundError:
        print(f"⚠️ 스키마 파일을 찾을 수 없습니다: {schema_file_path}")
        print("기본 AlarmValue 속성들을 사용합니다.")
        alarm_value_properties = {"state", "id", "isEnabled", "hour", "minutes", "label", "daysOfWeek"}
    except json.JSONDecodeError as e:
        print(f"⚠️ JSON 파싱 오류: {e}")
        alarm_value_properties = {"state", "id", "isEnabled", "hour", "minutes", "label", "daysOfWeek"}
    data_schema_properties = alarm_value_properties


    # 샘플 파일 설정 (간단 테스트용)
    sample_files = [
        (os.path.join(app_source_code_dir, "app", "src", "main", "java", "com", "better", "alarm", "ui", "row", "RowHolder.kt"), "kotlin"),
        (os.path.join(app_source_code_dir, "app", "src", "main", "java", "com", "better", "alarm", "ui", "list", "AlarmListAdapter.kt"), "kotlin"),
        # (os.path.join(app_source_code_dir, "app", "src", "main", "java", "com", "better", "alarm", "ui", "list", "AlarmsListFragment.kt"), "kotlin"),
    ]
    
    # print("=== AST 구조 분석 ===")
    # for file_path, language in sample_files:
    #     parser.debug_ast_structure(file_path, language)

    all_r_id_findings = {}
    all_findviewbyid_findings = {}
    all_wrapper_fn_findings = {}
    all_indirect_call_findings = {}
    all_assignment_findings = {}
    all_viewholder_findings = {}
    all_rendering_method_findings = {}
    all_interaction_findings = {}
    
    print("\n--- [Phase 1] ViewHolder 정보 수집 중... ---")
    project_viewholders_map = {} # key: class_name, value: {properties: set(), ...}
    parsed_data_cache = {} 

    for file_path, language in source_files: 
        if file_path not in parsed_data_cache:
            parsed_data_cache[file_path] = parser.parse_file(file_path, language)
        parsed_data = parsed_data_cache[file_path]
        if not parsed_data['success']: continue

        viewholder_results = parser.find_view_holders(parsed_data)
        if viewholder_results:
            for holder in viewholder_results:
                class_name = holder['class_name']
                properties = {prop['name'] for prop in holder['properties']}
                project_viewholders_map[class_name] = {"properties": properties, "node": holder['node'], "file_path": file_path}

    print(f"--- [Phase 1] 완료. 총 {len(project_viewholders_map)}개의 ViewHolder 식별: {set(project_viewholders_map.keys())} ---")
    print("\n--- [Phase 2 & 3] 샘플 파일 분석 시작 ---")
    
    for file_path, language in sample_files:
        filename = os.path.basename(file_path)

        print(f"\n=============================================")
        print(f"{os.path.basename(file_path)} 분석 시작")
        print(f"=============================================")

        if file_path in parsed_data_cache:
            parsed_data = parsed_data_cache[file_path]
        else:
            parsed_data = parser.parse_file(file_path, language)
        
        if not parsed_data['success']:
            print(f"[!] 파싱 실패: {file_path} ({parsed_data['error']})")
            continue
            
        r_id_results = parser.find_ui_elements(parsed_data) # R.id.xxx 분석
        if r_id_results:
            print(f"\n--- [R.id] {filename} ---")
            for finding in r_id_results:
                print(f"  [Line {finding['line']}]: {finding['text']}")
            all_r_id_findings[file_path] = r_id_results
            
        findview_results = parser.find_findviewbyid_direct_calls(parsed_data) # findViewById 직접 호출에 해당하는 케이스 분석
        if findview_results:
            print(f"\n--- [findViewById direct calls] {filename} ---")
            for finding in findview_results:
                print(f"  [Line {finding['line']}]: {finding['text']}")
            all_findviewbyid_findings[file_path] = findview_results
        
        wrapper_fn_results = parser.find_wrapper_functions_for_findviewbyid(parsed_data) # 래퍼 함수 정의 분석
        if wrapper_fn_results:
            print(f"\n--- [Wrapper Function Definition (for findViewById)] {filename} ---")
            for finding in wrapper_fn_results:
                print(f"  [Line {finding['line']}]: Found wrapper function '{finding['wrapper_name']}'")
                for internal_call in finding['internal_calls']:
                    print(f"    -> Wraps: [Line {internal_call['line']}] {internal_call['text']}")
            all_wrapper_fn_findings[file_path] = wrapper_fn_results

        if wrapper_fn_results:  # 래퍼 함수가 발견된 경우에만 간접 호출 탐색
            wrapper_names_set = {finding['wrapper_name'] for finding in wrapper_fn_results}
            
            indirect_call_results = parser.find_indirect_wrapper_function_calls(parsed_data, wrapper_names_set)
            if indirect_call_results:
                print(f"\n--- [Indirect Calls (Wrapper Usage - for findViewById)] {filename} ---")
                for finding in indirect_call_results:
                    print(f"  [Line {finding['line']}]: {finding['text']} (calls wrapper: {finding['wrapper_name']})")
                all_indirect_call_findings[file_path] = indirect_call_results
        
        assignment_results = parser.find_uisink_datasource_assignments(parsed_data) # 할당 구문 분석 (UI-Data 매핑)
        if assignment_results:
            print(f"\n--- [UI-Data Assignments] {filename} ---")
            for finding in assignment_results:
                print(f"  [Line {finding['line']}]: {finding['ui_sink']} = {finding['data_source']}")
            all_assignment_findings[file_path] = assignment_results
        
        viewholder_results = parser.find_view_holders(parsed_data)
        if viewholder_results:
            print(f"\n--- [View Holder Analysis] {filename} ---")
            for holder in viewholder_results:
                print(f"  ViewHolder Class: {holder['class_name']}")
                print(f"    Properties:")
                for prop in holder['properties']:
                    type_info = f" (Type: {prop['type']})" if prop['type'] else ""
                    init_info = f" (Init: {prop['init']})" if prop['init'] else ""
                    analysis = f" -> Analysis: {prop['init_analysis']}" if prop['init_analysis'] else ""
                    print(f"      - {prop['name']}{type_info}{init_info}{analysis}")
            all_viewholder_findings[file_path] = viewholder_results

        # Phase 2: 뷰 렌더링 메서드 식별
        rendering_method_results = parser.find_view_rendering_methods(parsed_data, set(project_viewholders_map.keys()))
        if rendering_method_results:
            print(f"\n--- [View Rendering Methods] {filename} ---")
            all_rendering_method_findings[file_path] = rendering_method_results
            
            for method in rendering_method_results:
                print(f"  Method: {method['method_name']}")
                print(f"    - ViewHolder Param (Phase 2): {method['holder_param']}")
                print(f"    - Data Param (Phase 2): {method['data_param']}")
                
                # 🚀 --- Phase 3: 상호작용 분석 호출 ---
                
                # Phase 1에서 찾은 ViewHolder 속성 맵을 올바르게 구성
                view_holder_properties_map = {}
                for class_name, data in project_viewholders_map.items():
                    view_holder_properties_map[class_name] = data['properties']

                interaction_results = parser.analyze_view_and_data_interactions_in_method(
                    parsed_data, 
                    method, 
                    view_holder_properties_map,  
                    data_schema_properties
                )
                
                if interaction_results:
                    print(f"    Interactions (Phase 3):")
                    if file_path not in all_interaction_findings: all_interaction_findings[file_path] = []
                    all_interaction_findings[file_path].extend(interaction_results)
                    for interaction in interaction_results:
                        if interaction['type'] == 'assignment_direct':
                            print(f"      - [Assign Direct] {interaction['sink']} = {interaction['source']} (Line {interaction['line']})")
                        elif interaction['type'] == 'assignment_indirect_call':
                            print(f"      - [Assign Indirect] {interaction['sink']} = {interaction['source_call']} (Line {interaction['line']})")
                        elif interaction['type'] == 'call_interaction':
                            print(f"      - [Call] {interaction['full_text']} (Line {interaction['line']})")

    print("\n--- 분석 완료 ---")
    
    # 각 카테고리별 총 요소 개수 계산
    total_r_id_elements = sum(len(findings) for findings in all_r_id_findings.values())
    total_findviewbyid_calls = sum(len(findings) for findings in all_findviewbyid_findings.values())
    total_wrapper_functions = sum(len(findings) for findings in all_wrapper_fn_findings.values())
    total_indirect_calls = sum(len(findings) for findings in all_indirect_call_findings.values())
    total_assignments = sum(len(findings) for findings in all_assignment_findings.values())
    total_viewholders = sum(len(findings) for findings in all_viewholder_findings.values())
    total_rendering_methods = sum(len(findings) for findings in all_rendering_method_findings.values())
    total_interactions = sum(len(findings) for findings in all_interaction_findings.values())
    
    print(f"발견된 R.id 요소 개수: {total_r_id_elements}개")
    print(f"발견된 findViewById 직접 호출 개수: {total_findviewbyid_calls}개")
    print(f"발견된 Wrapper 함수 정의 개수: {total_wrapper_functions}개")
    print(f"발견된 Wrapper 함수 간접 호출 개수: {total_indirect_calls}개")
    print(f"발견된 UI-Data 할당 구문 개수: {total_assignments}개")
    print(f"발견된 ViewHolder 클래스 개수: {total_viewholders}개")
    print(f"발견된 View Rendering 메서드 개수: {total_rendering_methods}개")
    print(f"발견된 렌더링 메서드 내 UI-Data interactions 개수: {total_interactions}개")
    
    total_direct_calls = total_findviewbyid_calls
    
    if total_direct_calls > 0 or total_indirect_calls > 0:
        print(f"\n--- findViewById 사용량 요약 ---")
        print(f"직접 호출 (findViewById): {total_direct_calls}개")
        print(f"간접 호출 (래퍼 함수): {total_indirect_calls}개")
        print(f"전체 findViewById 사용: {total_direct_calls + total_indirect_calls}개")