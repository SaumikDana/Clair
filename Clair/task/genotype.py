from enum import IntEnum


class Genotype(IntEnum):
    homo_reference = 0          # 0/0
    homo_variant = 1            # 1/1
    hetero_variant = 2          # 0/1 OR (1/2 for genotype task)
    hetero_variant_multi = 3    # 1/2


def genotype_string_from(genotype):
    if genotype == Genotype.homo_reference:
        return "0/0"
    if genotype == Genotype.homo_variant:
        return "1/1"
    if genotype == Genotype.hetero_variant:
        return "0/1"
    if genotype == Genotype.hetero_variant_multi:
        return "1/2"
    return ""


def genotype_enum_from(genotype_1, genotype_2):
    if genotype_1 == 0 and genotype_2 == 0:
        return Genotype.homo_reference
    if genotype_1 == genotype_2:
        return Genotype.homo_variant
    if genotype_1 != 0 and genotype_2 != 0:
        return Genotype.hetero_variant_multi
    return Genotype.hetero_variant


def genotype_enum_for_task(genotype):
    if genotype == Genotype.hetero_variant_multi:
        return Genotype.hetero_variant
    return genotype
