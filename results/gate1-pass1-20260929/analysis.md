## W2.X1 / default  (controlled, PASS)
records 240, conformant 240, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| tail-depth-4 | 792.0 | 1425.8 | 1456.9 | 1.8 | 1.02 | 366/511/676 |
| incoming | 1040.6 | 1654.9 | 1745.3 | 1.59 | 1.05 | 435/620/785 |
| outgoing | 1741.4 | 2341.3 | 2623.6 | 1.34 | 1.12 | 644/965/1178 |
| depth-4 | 1646.3 | 2236.2 | 2522.5 | 1.36 | 1.13 | 619/927/1165 |
| depth-2 | 1673.7 | 2277.8 | 2529.7 | 1.36 | 1.11 | 619/927/1165 |
| mixed-relations | 1704.3 | 2280.0 | 2560.1 | 1.34 | 1.12 | 624/934/1172 |
| depth-8 | 1643.0 | 2251.3 | 2524.5 | 1.37 | 1.12 | 619/927/1165 |
| tail-outgoing | 780.3 | 1405.2 | 1465.2 | 1.8 | 1.04 | 366/511/676 |

## W2.X2 / default  (controlled, PASS)
records 210, conformant 210, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| capped-depth-64 | 133.8 | 186.0 | 194.3 | 1.39 | 1.04 | 81/112/136 |
| incoming | 110.2 | 186.8 | 192.5 | 1.7 | 1.03 | 81/112/136 |
| outgoing | 123.4 | 184.9 | 192.4 | 1.5 | 1.04 | 81/112/136 |
| mixed-relations-64 | 121.0 | 185.3 | 191.9 | 1.53 | 1.04 | 81/112/136 |
| depth-2 | 120.4 | 185.4 | 192.3 | 1.54 | 1.04 | 81/112/136 |
| depth-64 | 123.0 | 187.4 | 192.2 | 1.52 | 1.03 | 81/112/136 |
| depth-8 | 135.2 | 188.1 | 195.4 | 1.39 | 1.04 | 81/112/136 |

## W2.X3 / default  (controlled, PASS)
records 180, conformant 180, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| outgoing | 753.1 | 1282.5 | 1326.0 | 1.7 | 1.03 | 362/487/657 |
| incoming | 759.6 | 1291.2 | 1334.8 | 1.7 | 1.03 | 362/487/657 |
| mixed-relations | 769.9 | 1296.0 | 1336.5 | 1.68 | 1.03 | 362/487/657 |
| depth-2 | 789.6 | 1301.9 | 1347.2 | 1.65 | 1.03 | 362/487/657 |
| depth-8 | 876.3 | 1384.6 | 1448.7 | 1.58 | 1.05 | 395/523/693 |
| depth-4 | 791.0 | 1317.0 | 1363.6 | 1.66 | 1.04 | 363/488/658 |

## W2.X4 / default  (controlled, PASS)
records 270, conformant 270, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| depth-2 | 821.2 | 1590.8 | 1649.9 | 1.94 | 1.04 | 437/577/780 |
| lookup-missing | 826.8 | 1571.8 | 1619.7 | 1.9 | 1.03 | 436/576/779 |
| outgoing | 840.1 | 1597.4 | 1647.7 | 1.9 | 1.03 | 436/576/779 |
| scan-type | 1478.5 | 2172.6 | 2379.3 | 1.47 | 1.1 | 640/900/1103 |
| incoming | 863.4 | 1606.0 | 1663.4 | 1.86 | 1.04 | 437/576/779 |
| depth-4 | 1023.6 | 1729.3 | 1833.2 | 1.69 | 1.06 | 489/656/859 |
| mixed-relations | 912.8 | 1633.6 | 1685.3 | 1.79 | 1.03 | 441/581/784 |
| depth-8 | 1933.4 | 2566.7 | 2860.4 | 1.33 | 1.11 | 750/1083/1335 |
| lookup-first | 884.1 | 1605.1 | 1664.5 | 1.82 | 1.04 | 436/576/779 |

## W2.X5 / default  (controlled, PASS)
records 300, conformant 300, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| out-cap-below | 996.4 | 1537.4 | 1603.1 | 1.54 | 1.04 | 422/557/728 |
| in-cap-below | 1077.3 | 1634.1 | 1735.9 | 1.52 | 1.06 | 452/608/778 |
| out-cap-1 | 847.7 | 1392.5 | 1427.1 | 1.64 | 1.02 | 377/490/660 |
| in-cap-above | 1068.5 | 1639.3 | 1726.8 | 1.53 | 1.05 | 452/608/778 |
| out-cap-2 | 833.3 | 1403.8 | 1433.8 | 1.68 | 1.02 | 377/490/660 |
| out-cap-exact | 1004.1 | 1535.6 | 1605.7 | 1.53 | 1.05 | 422/557/728 |
| in-cap-1 | 847.6 | 1404.2 | 1441.8 | 1.66 | 1.03 | 377/490/660 |
| out-cap-above | 977.7 | 1526.9 | 1614.9 | 1.56 | 1.06 | 422/557/728 |
| in-cap-exact | 1060.8 | 1649.9 | 1744.0 | 1.56 | 1.06 | 452/608/778 |
| in-cap-2 | 928.3 | 1460.0 | 1499.3 | 1.57 | 1.03 | 377/490/660 |

## W3.S1 / default  (controlled, PASS)
records 240, conformant 240, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| verified-epoch-1 | 888.3 | 1491.7 | 1545.3 | 1.68 | 1.04 | 430/556/723 |
| min-exact | 1179.6 | 1758.1 | 1895.3 | 1.49 | 1.08 | 508/703/871 |
| sel-100 | 1800.0 | 2341.7 | 2669.0 | 1.3 | 1.14 | 689/1000/1240 |
| epoch-1 | 1301.1 | 1857.7 | 2020.0 | 1.43 | 1.09 | 538/751/919 |
| min-verified | 984.2 | 1568.3 | 1666.2 | 1.59 | 1.06 | 449/606/773 |
| epoch-3 | 839.5 | 1412.7 | 1461.9 | 1.68 | 1.03 | 389/511/679 |
| epoch-2 | 917.5 | 1491.6 | 1555.1 | 1.63 | 1.04 | 429/556/723 |
| epoch-4 | 811.9 | 1397.1 | 1448.5 | 1.72 | 1.04 | 384/507/674 |

## W3.S2 / default  (controlled, PASS)
records 180, conformant 180, samples with condition failures 0, sdk_workaround [False]
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| sel-100 | 1959.0 | 2450.6 | 2776.1 | 1.25 | 1.13 | 687/990/1235 |
| min-verified | 1093.9 | 1642.7 | 1729.3 | 1.5 | 1.05 | 447/599/771 |
| hub-depth-4 | 1018.0 | 1555.1 | 1613.6 | 1.53 | 1.04 | 422/557/729 |
| min-exact | 1402.2 | 1937.2 | 2090.2 | 1.38 | 1.08 | 537/745/917 |
| hub-outgoing | 875.8 | 1443.7 | 1478.7 | 1.65 | 1.02 | 379/492/664 |
| epoch-1 | 1238.7 | 1766.8 | 1887.9 | 1.43 | 1.07 | 487/664/836 |

## W4.S1 / default  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 152.4 | 179.7 | - | 1.18 | - | 82/131/- |

## W4.S2 / default  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 141.1 | 177.9 | - | 1.26 | - | 82/131/- |

## W4.S3 / default  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 90.3 | 139.0 | - | 1.54 | - | 63/81/- |

## W4.S4 / r1  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 88.1 | 138.3 | - | 1.57 | - | 63/80/- |

## W4.S4 / r10  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 95.6 | 143.6 | - | 1.5 | - | 64/82/- |

## W4.S4 / r100  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 138.3 | 175.7 | - | 1.27 | - | 82/131/- |

## W4.S4 / r50  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 112.9 | 160.9 | - | 1.43 | - | 71/103/- |

## W4.S5 / len512-uniform  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 233.4 | 348.0 | - | 1.49 | - | 268/218/- |

## W4.S5 / len64-uniform  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 121.8 | 176.5 | - | 1.45 | - | 89/113/- |

## W4.S5 / len64-zipf  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 365.5 | 258.3 | - | 0.71 | - | 85/96/- |

## W4.S5 / len8-uniform  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 371.6 | 146.7 | - | 0.39 | - | 68/100/- |

## W4.S6 / default  (controlled, PASS)
records 20, conformant 20, samples with condition failures 0, sdk_workaround [False]
not run: {'hybrid': 'W4 needs in-process phase/heap instrumentation; the hybrid is a whole-fixture FFI batch (ADR-0007)'}
| query | rust ms | zig ms | hybrid ms | zig/rust | hybrid/zig | rss MB r/z/h |
|---|---|---|---|---|---|---|
| resolve-primary | 1102.6 | 522.8 | - | 0.47 | - | 69/88/- |

