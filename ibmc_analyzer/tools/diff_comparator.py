import os
import glob
import json
import difflib
from .log_extractor import iBMCLogExtractor

def compare_json_configs(dict_a: dict, dict_b: dict) -> dict:
    """处理 JSON 类型配置的深度比对 (如 currentvalue.json)"""
    diff_result = {"only_in_A": {}, "only_in_B": {}, "value_mismatch": {}}
    all_keys = set(dict_a.keys()).union(set(dict_b.keys()))
    
    for k in all_keys:
        val_a = dict_a.get(k)
        val_b = dict_b.get(k)
        if k not in dict_b:
            diff_result["only_in_A"][k] = val_a
        elif k not in dict_a:
            diff_result["only_in_B"][k] = val_b
        elif val_a != val_b:
            diff_result["value_mismatch"][k] = {"Node_A": val_a, "Node_B": val_b}
            
    # 清理空结果
    return {k: v for k, v in diff_result.items() if v}

def compare_text_configs(text_a: str, text_b: str) -> str:
    """处理纯文本的智能 Diff (过滤无意义的时间戳差异)"""
    lines_a = [l.strip() for l in text_a.split('\n') if l.strip()]
    lines_b = [l.strip() for l in text_b.split('\n') if l.strip()]
    
    diff = difflib.unified_diff(
        lines_a, lines_b, 
        fromfile='Node_A', tofile='Node_B', 
        n=0 # 不输出多余的上下文，只输出变化的行
    )
    diff_text = '\n'.join(diff)
    return diff_text if diff_text else "No differences found."

def run_node_comparison(dump_path_a: str, dump_path_b: str, file_pattern: str) -> str:
    """
    针对指定的配置文件模式，比对两个 dump 包的区别。
    例如 file_pattern = "**/currentvalue.json" 或 "**/sysinfo/cmdline"
    """
    def get_single_file_content(base_path):
        search_path = os.path.join(base_path, file_pattern)
        matched = [f for f in glob.glob(search_path, recursive=True) if not f.endswith(('.md5', '.sha256'))]
        if not matched:
            return None, f"File matching {file_pattern} not found."
        # 使用 Extractor 的安全读取
        extractor = iBMCLogExtractor(base_path)
        return matched[0], extractor._read_file_content(matched[0])

    file_a, content_a = get_single_file_content(dump_path_a)
    file_b, content_b = get_single_file_content(dump_path_b)

    if not file_a or not file_b:
        return json.dumps({"error": "Missing files in one or both directories", "A": file_a, "B": file_b})

    # 判断是否为 JSON
    if file_a.endswith('.json') and file_b.endswith('.json'):
        try:
            json_a = json.loads(content_a)
            json_b = json.loads(content_b)
            diff_res = compare_json_configs(json_a, json_b)
            return json.dumps(diff_res, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass # 回退到文本比对
            
    return compare_text_configs(content_a, content_b)