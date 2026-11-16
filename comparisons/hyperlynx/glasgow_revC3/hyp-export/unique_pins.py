"""Copy of a KiCad board where no footprint has two pads with the same number on the
same net: extra plated-through pads become board vias, extra SMD pads become copper
zones of the same shape. For exporters (HyperLynx .hyp) that key pins by refdes.pin."""
import sys, collections, pcbnew
src, dst = sys.argv[1:3]
b = pcbnew.LoadBoard(src)
log = []
for fp in b.GetFootprints():
    groups = collections.defaultdict(list)
    for p in fp.Pads():
        if p.GetNumber() and p.GetNetCode() > 0:
            groups[p.GetNumber()].append(p)
    for num, pads in groups.items():
        if len(pads) < 2:
            continue
        # keep one SMD pad if there is one (the pin's own land), else the first
        keep = next((p for p in pads if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD),
                    max(pads, key=lambda p: p.GetSize().x * p.GetSize().y))
        # only round plated holes become vias; slots (shield tabs) are left as they are
        if any(p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH and
               p.GetDrillSize().x != p.GetDrillSize().y for p in pads):
            continue
        for p in pads:
            if p is keep:
                continue
            ref = fp.GetReference()
            if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                v = pcbnew.PCB_VIA(b)
                v.SetPosition(p.GetPosition())
                v.SetWidth(max(p.GetSize().x, p.GetSize().y))
                v.SetDrill(min(p.GetDrillSize().x, p.GetDrillSize().y))
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                v.SetNetCode(p.GetNetCode())
                b.Add(v)
                log.append(f"{ref} pad {num} PTH -> via at {pcbnew.ToMM(p.GetPosition().x):.3f},{pcbnew.ToMM(p.GetPosition().y):.3f} on {p.GetNetname()}")
            else:
                for layer in p.GetLayerSet().CuStack():
                    poly = pcbnew.SHAPE_POLY_SET()
                    p.TransformShapeToPolygon(poly, layer, 0, pcbnew.FromMM(0.001))
                    z = pcbnew.ZONE(b)
                    z.SetLayer(layer)
                    z.SetNetCode(p.GetNetCode())
                    z.SetAssignedPriority(100)
                    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
                    z.SetMinThickness(pcbnew.FromMM(0.01))
                    z.SetIsFilled(True)
                    z.SetZoneName(f"{ref}.{num} land")
                    z.Outline().RemoveAllContours()
                    z.Outline().Append(poly)
                    z.SetFilledPolysList(layer, poly)
                    b.Add(z)
                log.append(f"{ref} pad {num} SMD -> copper zone on {p.GetLayerName()} on {p.GetNetname()}")
            fp.Remove(p)
pcbnew.SaveBoard(dst, b)
print("\n".join(log)); print(len(log), "pads replaced")
