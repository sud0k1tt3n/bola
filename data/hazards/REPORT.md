# Отчёт о подготовке датасета

Источник: `/mnt/user-data/uploads/task_2_dataset_2026_09_28_18_40_14_yolo_1.zip`

## Классы разметки → классы модели

| В разметке | Рамок | Стало |
|---|---|---|
| collapsed_bridge | 3 | bridge_collapse |
| damaged_bridge | 6 | bridge_collapse |
| flooded_road | 78 | flooding |
| flooded_area | 2 | flooding |
| fallen_tree_on_road | 60 | fallen_tree |
| fallen_tree_off_road | 33 | — выброшен |
| snow_drift | 1 | — выброшен |
| ice_jam | 0 | — выброшен |
| normal_bridge | 13 | — выброшен |

## Итог

| Часть | Кадров | flooding | bridge_collapse | fallen_tree |
|---|---|---|---|---|
| train | 106 | 66 | 7 | 47 |
| val | 27 | 14 | 2 | 13 |

Кадров без разметки в экспорте: 317 (не взяты).
Групп почти-дублей (держатся в одной части): 0.

## Предупреждения

- 317 кадров без разметки не взяты в обучение (список — unlabeled.txt). Доразметьте их или, если опасностей там точно нет, запустите с --keep-empty.
- Класс bridge_collapse: всего 9 рамок — для устойчивого обучения нужно от ~150–200.
