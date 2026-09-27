import os
import json
import zipfile
import subprocess
from pcbai.steps.kicad_schematic_writer import generate_schematic
from pcbai.steps.kicad_pcb_writer import generate_pcb

def compile_design(prompt: str, output_dir: str) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    
    import shutil
    template_dir = os.path.join(os.path.dirname(__file__), "template_project")
    
    # Copy all files from template_dir to output_dir
    for item in os.listdir(template_dir):
        s = os.path.join(template_dir, item)
        d = os.path.join(output_dir, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
            
    bom_path = os.path.join(output_dir, "bom.json")
    sch_path = os.path.join(output_dir, "schematic.kicad_sch")
    pcb_path = os.path.join(output_dir, "board.kicad_pcb")
    gerbers_dir = os.path.join(output_dir, "gerbers")
    zip_path = os.path.join(output_dir, "pcb_project.zip")
    
    with open(bom_path, "r", encoding="utf-8") as f:
        bom = json.load(f)
                
    return {
        "bom": bom,
        "sch": sch_path,
        "pcb": pcb_path,
        "gerbers": gerbers_dir,
        "zip": zip_path
    }
