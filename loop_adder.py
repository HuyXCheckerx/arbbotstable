import sys

def wrap_in_loop(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()
        
    out_lines = []
    in_main = False
    loop_started = False
    
    for i, line in enumerate(lines):
        if 'def main():' in line:
            in_main = True
            
        if in_main and 'prev_port = print_portfolio' in line and not loop_started:
            out_lines.append(line)
            out_lines.append('    print("[*] Starting Arb Bot loop...")\n')
            out_lines.append('    while True:\n')
            out_lines.append('        try:\n')
            loop_started = True
            continue
            
        if in_main and loop_started:
            if line.startswith('if __name__ == "__main__":'):
                # end of main, close loop
                out_lines.append('            print("[*] Waiting 10s before next check...")\n')
                out_lines.append('            time.sleep(10)\n')
                out_lines.append('        except Exception as e:\n')
                out_lines.append('            print(f"[!] Loop error: {e}")\n')
                out_lines.append('            time.sleep(10)\n\n')
                in_main = False
                out_lines.append(line)
            else:
                if line.strip() == '':
                    out_lines.append(line)
                else:
                    # replace return with continue
                    l = line.replace('return\n', 'continue\n')
                    # count leading spaces to preserve relative indentation, but we just want to add 8 spaces
                    # Wait, the original code inside main is indented by 4 spaces.
                    # Now it will be inside try, which is indented by 8 spaces.
                    # So we need to add 4 spaces to everything.
                    if l.startswith('    '):
                        out_lines.append('    ' + l)
                    else:
                        out_lines.append('        ' + l.lstrip(' '))
        else:
            out_lines.append(line)
            
    with open(filepath, 'w') as f:
        f.writelines(out_lines)
        
wrap_in_loop('swapstable.py')
wrap_in_loop('swapstable_pyusd.py')
print('Done looping')
